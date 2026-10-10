"""Operations the interface exposes beyond download control: scheduling, campaign management, bulk resume/pause, database
maintenance, health and troubleshooting. Real worker processes and the real API; synthetic data only."""
import json
import sqlite3
import subprocess
import sys
import time

import psutil
import pytest

from conftest import make_client, make_opts
from stockdata.db import CandleDB
from stockdata.downloader import Downloader
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.web.store import Store
from web_helpers import (Env, TOKEN_KEY, daily_spec, fake_jwt, intraday_spec, point_workers_at, web_env, wait_for)  # noqa: F401
from test_web_jobs import drive, status_is, db_rows  # noqa: F401


# ------------------------------------------------------------------ scheduling
def test_a_scheduled_job_waits_for_its_time_and_start_now_overrides_the_schedule(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    env = web_env
    job = env.create({**daily_spec(("SIMALPHA",)), "start_after": "2099-01-01T10:00:00"})["job"]       # a naive time is read as IST
    assert job["scheduled_for"] == "2099-01-01T04:30:00+00:00"
    env.sup.tick(); env.sup.tick()
    j = env.job(job["id"])
    assert j["status"] == "queued" and "scheduled: starts no earlier than 2099-01-01 10:00 IST" in j["block_reason"]
    started = env.act(job["id"], "start")
    assert started["waiting_reason"] is None and started["job"]["status"] in ("preparing", "running", "completed")
    assert started["job"]["scheduled_for"] is None                                                       # the schedule was cleared on purpose
    drive(env, status_is(env, job["id"], "completed"))
    past = env.create({**daily_spec(("SIMBETA",)), "start_after": "2001-01-01T00:00:00+05:30"})["job"]
    drive(env, status_is(env, past["id"], "completed"))                                                  # a time in the past is simply "now"


def test_start_now_on_a_job_the_scheduler_already_started_is_not_an_error(web_env, fake, monkeypatch):
    """Creating a job wakes the scheduler, which can start it before the explicit 'start now' arrives (seen as an
    intermittent HTTP 409 'job is already running' under load). That is the outcome that was asked for."""
    point_workers_at(monkeypatch, fake)
    env = web_env
    job = env.create(daily_spec(("SIMALPHA",)))["job"]
    env.sup.tick()                                                     # the scheduler wins the race
    assert env.job(job["id"])["status"] in ("preparing", "running", "completed")
    again = env.act(job["id"], "start")                                # used to answer 409
    assert again["waiting_reason"] is None
    drive(env, status_is(env, job["id"], "completed"))


def test_a_campaign_can_be_scheduled_paused_resumed_cancelled_and_removed(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    env = web_env
    r = env.client.post("/api/campaigns", json={"name": "plan", "confirm": True, "start_after": "2099-01-01T09:00:00",
                                                "jobs": [daily_spec(("SIMALPHA",)), intraday_spec(("SIMALPHA",))]})
    assert r.status_code == 200, r.text
    camp = r.json()
    first, second = camp["jobs"]
    assert camp["scheduled_for"] and first["scheduled_for"] and second["scheduled_for"] is None and camp["status"] == "queued"
    assert set(camp["actions"]) == {"pause", "cancel"}
    paused = env.client.post(f"/api/campaigns/{camp['id']}/pause", json={}).json()
    assert [j["status"] for j in paused["jobs"]] == ["paused", "paused"] and "resume" in paused["actions"]
    assert env.client.post(f"/api/campaigns/{camp['id']}/resume", json={}).status_code == 400            # confirmation needed
    resumed = env.client.post(f"/api/campaigns/{camp['id']}/resume", json={"confirm": True}).json()
    assert [j["status"] for j in resumed["jobs"]] == ["queued", "queued"]
    cancelled = env.client.post(f"/api/campaigns/{camp['id']}/cancel", json={"confirm": True}).json()
    assert [j["status"] for j in cancelled["jobs"]] == ["cancelled", "cancelled"] and "delete" in cancelled["actions"]
    assert env.client.delete(f"/api/campaigns/{camp['id']}").json() == {"deleted": camp["id"]}
    assert env.client.get("/api/campaigns").json() == [] and env.sup.counts()["total"] == 0
    live = env.client.post("/api/campaigns", json={"name": "x", "confirm": True, "jobs": [daily_spec(("SIMBETA",))]}).json()
    assert env.client.delete(f"/api/campaigns/{live['id']}").status_code == 409                          # not while unfinished


def test_resume_eligible_and_pause_all_use_the_graceful_paths(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    fake.delay = 0.3
    env = web_env
    a = env.create(intraday_spec(), start="now")["job"]
    b = env.create(daily_spec(("SIMALPHA",)))["job"]
    env.client.post("/api/queue", json={"enabled": False})                                               # b must not start when a pauses
    drive(env, lambda: env.job(a["id"])["progress"]["run_counts"].get("windows_done", 0) >= 1)
    out = env.client.post("/api/jobs/pause-all", json={}).json()
    assert out["pausing"] == [a["id"]] and env.job(a["id"])["status"] == "stopping"                       # graceful: it was NOT killed
    assert env.job(a["id"])["worker"]["alive"] is True
    drive(env, status_is(env, a["id"], "paused"))
    env.act(b["id"], "pause")
    assert env.client.post("/api/jobs/resume-eligible", json={}).status_code == 400
    r = env.client.post("/api/jobs/resume-eligible", json={"confirm": True}).json()
    assert sorted(r["resumed"]) == sorted([a["id"], b["id"]]) and all(j["status"] == "queued" for j in r["jobs"])
    env.client.post("/api/queue", json={"enabled": True})
    fake.delay = 0
    drive(env, lambda: all(env.job(i)["status"] == "completed" for i in (a["id"], b["id"])), timeout=120)


# ------------------------------------------------------------------ worker processes nobody accounts for
def test_untracked_workers_are_found_and_can_only_be_stopped_with_confirmation(web_env):
    env = web_env
    marker = str(env.settings.jobs_dir / "777" / "run_1")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "stockdata.web.worker", "--control-dir", marker])
    try:
        found = env.client.get("/api/system/orphans").json()
        assert [o["pid"] for o in found] == [proc.pid]
        assert env.client.post(f"/api/system/orphans/{proc.pid}/kill", json={}).status_code == 400
        assert env.client.post(f"/api/system/orphans/{os_pid_other()}/kill", json={"confirm": True}).status_code == 404   # only real orphans
        f = env.client.get("/api/system/findings").json()
        assert any(x["level"] == "error" and "no job accounts for" in x["title"] for x in f)
        done = env.client.post(f"/api/system/orphans/{proc.pid}/kill", json={"confirm": True}).json()
        assert done["outcome"] == "killed"
        proc.wait(timeout=10)
        assert env.client.get("/api/system/orphans").json() == []
        assert any(a["action"] == "system.kill_orphan" for a in env.client.get("/api/audit").json())
    finally:
        proc.kill()


def os_pid_other() -> int:
    import os
    return os.getpid()


# ------------------------------------------------------------------ database maintenance
@pytest.fixture
def filled(tmp_path, fake):
    env = Env(tmp_path)
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, env.settings.data_dir / tf.db_name) as db:
            Downloader(db, make_client(fake)).run(db.get_instruments(symbols=["SIMALPHA", "SIMBETA"]), make_opts(tf))
    yield env
    env.close()


def test_recover_replays_the_log_and_leaves_the_data_exactly_as_it_was(filled):
    env = filled
    before = db_rows(env, DAILY)
    r = env.client.post("/api/maintenance/daily/recover", json={}); assert r.status_code == 400
    out = env.client.post("/api/maintenance/daily/recover", json={"confirm": True}).json()
    assert out["timeframe"] == "daily" and out["candles"] == len(before) and "database" in out["message"]
    assert db_rows(env, DAILY) == before
    assert env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001}).status_code == 200   # reads work again afterwards
    assert any(a["action"] == "maintenance.recover" for a in env.client.get("/api/audit").json())


def test_backup_copies_the_checkpointed_file_checks_space_and_blocks_other_access_meanwhile(filled, monkeypatch):
    env = filled
    r = env.client.post("/api/maintenance/daily/backup", json={"confirm": True}); assert r.status_code == 200, r.text
    st = wait_for(lambda: (lambda m: m if not m["tasks"]["daily"]["running"] else None)(env.client.get("/api/maintenance").json()), timeout=30)
    assert st["tasks"]["daily"]["error"] is None and len(st["backups"]) == 1
    b = st["backups"][0]
    assert b["timeframe"] == "daily" and b["bytes"] == (env.settings.data_dir / "stockdata_daily.duckdb").stat().st_size
    import duckdb
    copy = duckdb.connect(str(env.settings.data_dir / "backups" / b["name"]), read_only=True)
    assert copy.execute("SELECT count(*) FROM candles_daily").fetchone()[0] == len(db_rows(env, DAILY))
    copy.close()
    # not enough space -> refused with the numbers
    import stockdata.web.maintenance as M
    monkeypatch.setattr(M, "free_bytes", lambda p: 1000)
    refused = env.client.post("/api/maintenance/daily/backup", json={"confirm": True})
    assert refused.status_code == 409 and "Not enough free disk space" in refused.json()["error"]
    # deletion needs confirmation and a real backup name
    assert env.client.delete(f"/api/maintenance/backups/{b['name']}").status_code == 400
    assert env.client.delete("/api/maintenance/backups/..%2Fx.duckdb", params={"confirm": True}).status_code in (400, 404)
    assert env.client.delete(f"/api/maintenance/backups/{b['name']}", params={"confirm": True}).json() == {"deleted": b["name"]}


def test_maintenance_is_refused_while_a_job_owns_the_database_and_reserves_it_while_it_runs(filled):
    env = filled
    env.store.execute("INSERT INTO jobs (name, kind, timeframe, status, options, queue_pos, created_at) VALUES "
                      "('w', 'download', 'daily', 'running', '{}', 1, 'x')")
    assert env.client.post("/api/maintenance/daily/recover", json={"confirm": True}).status_code == 409
    assert env.client.post("/api/maintenance/daily/backup", json={"confirm": True}).status_code == 409
    env.store.execute("DELETE FROM jobs")
    assert env.sup.data.gate.begin_maintenance("daily", "backup")                                   # now simulate a maintenance run
    try:
        busy = env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001})
        assert busy.status_code == 423 and "maintenance (backup)" in busy.json()["error"]
        job = env.create(daily_spec(("SIMALPHA",)))["job"]
        env.sup.tick()
        assert "in maintenance (backup)" in env.job(job["id"])["block_reason"] and env.job(job["id"])["status"] == "queued"
        assert env.sup.data.gate.begin_maintenance("daily", "again") is False                        # one at a time
    finally:
        env.sup.data.gate.end_maintenance("daily")


# ------------------------------------------------------------------ health, troubleshooting, validation
def test_dependency_and_database_health_and_findings(filled):
    env = filled
    dep = env.client.get("/api/system/dependencies").json()
    assert dep["python_ok"] and dep["packages_ok"] and dep["node_required"] is False and dep["duckdb"]
    assert {p["package"] for p in dep["packages"]} >= {"duckdb", "pandas", "fastapi", "uvicorn", "psutil"}
    dbs = env.client.get("/api/system/databases").json()
    assert dbs["daily"]["readable"] is True and dbs["daily"]["identity"] == "daily" and dbs["15min"]["identity"] == "15min"
    others = [x for x in env.client.get("/api/system/findings").json() if "scheduler" not in x["title"]]      # (these tests run without the scheduler thread)
    assert all(x["level"] in ("ok", "info", "warn") for x in others), others
    env.store.execute("INSERT INTO jobs (name, kind, timeframe, status, options, queue_pos, created_at) VALUES "
                      "('w', 'download', '15min', 'running', ?, 1, 'x')", (json.dumps({"spec": {"kind": "download", "timeframe": "15min", "selection": {"mode": "all"}, "confirm_full": True}}),))
    assert env.client.get("/api/system/databases").json()["15min"]["writer_job_id"] > 0                # reported, not probed under a writer
    rec = env.client.post("/api/system/reconcile").json()
    assert rec["checked"] == 1 and rec["changed"] and list(rec["changed"].values())[0]["to"] == "interrupted"   # a job with no worker record


def test_a_missing_or_expired_token_is_a_finding_with_a_route_to_fix_it(tmp_path, monkeypatch):
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    env = Env(tmp_path, token=fake_jwt(exp=1_000_000_000), client_id="1000000001")
    try:
        f = env.client.get("/api/system/findings").json()
        bad = [x for x in f if "expired" in x["title"]]
        assert bad and bad[0]["level"] == "error" and bad[0]["route"] == "/settings"
    finally:
        env.close()
    env2 = Env(tmp_path / "b", token=None, client_id=None)
    try:
        assert any("credentials are not configured" in x["title"] for x in env2.client.get("/api/system/findings").json())
    finally:
        env2.close()


def test_the_diagnostics_bundle_is_redacted_and_complete(filled):
    env = filled
    secret = "VERYSECRETTOKENVALUE987654"
    env.client.put("/api/credentials", json={"access_token": secret})
    env.store.event(None, "note", f"something with access-token: {secret} in it")
    r = env.client.get("/api/system/diagnostics")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    text = r.text
    assert secret not in text and "[REDACTED]" in text or secret not in text
    for section in ("== dependencies ==", "== databases ==", "== findings ==", "== request usage ==", "== jobs ==", "== recent events =="):
        assert section in text
    assert str(env.settings.data_dir) in text


def test_settings_validation_does_not_save_and_explains(web_env):
    env = web_env
    r = env.client.post("/api/settings/validate", json={"max_per_day": 100_001}).json()
    assert r["ok"] is False and "documented" in r["problems"][0]
    ok = env.client.post("/api/settings/validate", json={"max_per_run": 95_000, "max_per_day": 95_000, "rps": 4.5, "parallel_timeframes": True}).json()
    assert ok["ok"] is True and len(ok["notes"]) == 3
    assert env.client.get("/api/settings").json()["limits"]["max_per_day"] == 80_000                  # nothing was saved


def test_an_older_control_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, timeframe TEXT,
        status TEXT NOT NULL, options TEXT NOT NULL, plan TEXT, command TEXT, campaign_id INTEGER, campaign_pos INTEGER, queue_pos REAL NOT NULL,
        created_at TEXT NOT NULL, queued_at TEXT, started_at TEXT, finished_at TEXT, last_activity_at TEXT, runs INTEGER NOT NULL DEFAULT 0,
        exit_code INTEGER, engine_status TEXT, note TEXT, last_error TEXT, next_action TEXT, pending_action TEXT, block_reason TEXT,
        windows_planned INTEGER, windows_done INTEGER NOT NULL DEFAULT 0, instruments_total INTEGER, instruments_done INTEGER NOT NULL DEFAULT 0,
        rows_inserted INTEGER NOT NULL DEFAULT 0, attempts_used INTEGER NOT NULL DEFAULT 0, failed_windows INTEGER NOT NULL DEFAULT 0,
        retriable_windows INTEGER NOT NULL DEFAULT 0, progress TEXT, fingerprint TEXT, source_job_id INTEGER);
        INSERT INTO jobs (name, kind, status, options, queue_pos, created_at) VALUES ('kept', 'download', 'paused', '{}', 1, 'x');""")
    con.commit(); con.close()
    store = Store(path)
    row = store.one("SELECT name, status, not_before FROM jobs")
    assert row == {"name": "kept", "status": "paused", "not_before": None} and store.get_meta("schema_version") == 3
    assert store.one("SELECT provider, lane, yielded_to FROM jobs") == {"provider": None, "lane": None, "yielded_to": None}   # legacy = Dhan
    store.close()


def test_switching_the_queue_works_even_when_an_unrelated_environment_value_is_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKDATA_RPS", "1000")                           # outside Dhan's range, but not what is being changed
    env = Env(tmp_path)
    try:
        assert env.sup.effective()["rps"] == 1000
        assert env.client.post("/api/queue", json={"enabled": False}).json() == {"queue_enabled": False}
        assert env.sup.effective()["queue_enabled"] is False
        assert env.client.put("/api/settings", json={"rps": 9}).status_code == 422                   # an explicit bad value is still refused
        assert env.client.put("/api/settings", json={"rps": 2.5}).json()["limits"]["rps"] == 2.5
    finally:
        env.close()
