"""The job queue and worker supervision, with REAL worker processes running the real CLI against the local fake Dhan
server. No Dhan traffic, no production database or ledger."""
import json
import time
from datetime import date, datetime

import psutil
import pytest

from conftest import load_universe
from stockdata.budget import read_usage
from stockdata.db import CandleDB
from stockdata.timeframes import DAILY, INTRADAY_15
from web_helpers import (Env, daily_spec, intraday_spec, point_workers_at, sched_env, wait_for, web_env)  # noqa: F401


def drive(env, predicate, timeout=90, message="job state"):
    """Run scheduler ticks until the predicate holds (the real scheduler does exactly this every 2 seconds)."""
    def step():
        env.sup.tick()
        return predicate()
    return wait_for(step, timeout=timeout, interval=0.15, message=message)


def status_is(env, job_id, *statuses):
    return lambda: env.job(job_id)["status"] in statuses


def db_rows(env, tf):
    with CandleDB(tf, env.settings.data_dir / tf.db_name, read_only=True) as db:
        return db.con.execute(f"SELECT security_id, {tf.key_col}, open, high, low, close, volume FROM {tf.table} ORDER BY 1, 2").fetchall()


def window_states(env, tf):
    with CandleDB(tf, env.settings.data_dir / tf.db_name, read_only=True) as db:
        return dict(db.con.execute("SELECT status, count(*) FROM download_windows GROUP BY 1").fetchall())


def reference_rows(tmp_path, fake, tf, spec_start, spec_end, symbols):
    """What an uninterrupted engine run produces for the same range."""
    from conftest import make_client
    from stockdata.downloader import Downloader, RunOptions
    from stockdata.timeutil import to_midnight
    with CandleDB(tf, tmp_path / f"ref_{tf.db_name}") as db:
        load_universe(db)
        opts = RunOptions(history_start=to_midnight(spec_start), cutoff=to_midnight(spec_end), window_days=tf.default_window_days)
        n0 = len(fake.log)
        Downloader(db, make_client(fake)).run(db.get_instruments(symbols=list(symbols)), opts)
        rows = db.con.execute(f"SELECT security_id, {tf.key_col}, open, high, low, close, volume FROM {tf.table} ORDER BY 1, 2").fetchall()
        return rows, len(fake.log) - n0


def test_a_queued_job_runs_in_a_real_worker_and_its_progress_is_real(web_env, fake, monkeypatch, tmp_path):
    point_workers_at(monkeypatch, fake)
    env = web_env
    made = env.create(intraday_spec())
    job = made["job"]
    assert job["status"] == "queued" and job["command"].startswith("python -m stockdata --data-dir") and "--max-requests-per-run" in job["command"]
    assert "test-token" not in json.dumps(job)
    drive(env, status_is(env, job["id"], "completed", "completed_with_warnings", "failed"), message="completion")
    done = env.job(job["id"])
    assert done["status"] == "completed" and done["exit_code"] == 0 and done["engine_status"] == "completed"
    ref, ref_requests = reference_rows(tmp_path, fake, INTRADAY_15, date(2023, 6, 1), date(2024, 6, 3), ("SIMALPHA", "SIMBETA", "SIMGAMMA"))
    assert db_rows(env, INTRADAY_15) == ref                                              # same data as a plain engine run
    assert done["progress"]["windows_done"] == done["progress"]["windows_planned"] == ref_requests
    assert done["progress"]["fraction"] == 1.0 and done["rows_inserted"] == len(ref)
    assert done["attempts_used"] == ref_requests == sum(read_usage(env.settings.engine.request_ledger_path).values())   # every attempt is in the shared ledger
    log = env.client.get(f"/api/jobs/{job['id']}/log").json()["text"]
    assert "SIMALPHA" in log and "[1/3]" in log
    assert [e["type"] for e in env.client.get(f"/api/jobs/{job['id']}/events").json()][:2] == ["finished", "started"]


def test_pause_is_graceful_resume_continues_and_nothing_is_lost_or_duplicated(web_env, fake, monkeypatch, tmp_path):
    point_workers_at(monkeypatch, fake)
    fake.delay = 0.25
    env = web_env
    job = env.create(intraday_spec(), start="now")["job"]
    drive(env, lambda: env.job(job["id"])["progress"]["run_counts"].get("windows_done", 0) >= 2, message="two windows done")
    assert env.job(job["id"])["status"] == "running"
    paused = env.act(job["id"], "pause")["job"]
    assert paused["status"] == "stopping" and "in-flight" in paused["next_action"]
    drive(env, status_is(env, job["id"], "paused"), message="graceful stop")
    p = env.job(job["id"])
    assert p["exit_code"] == 5 and p["engine_status"] == "stopped_by_user" and "resume" in p["actions"]
    assert 2 <= p["windows_done"] < p["windows_planned"]
    states = window_states(env, INTRADAY_15)
    assert set(states) <= {"complete", "empty"} and sum(states.values()) == p["windows_done"]   # committed windows only, none half-done
    fake.delay = 0
    requests_before = len(fake.log)
    env.act(job["id"], "resume")
    drive(env, status_is(env, job["id"], "completed"), message="resumed completion")
    resume_requests = len(fake.log) - requests_before
    final = env.job(job["id"])
    ref, ref_requests = reference_rows(tmp_path, fake, INTRADAY_15, date(2023, 6, 1), date(2024, 6, 3), ("SIMALPHA", "SIMBETA", "SIMGAMMA"))
    assert db_rows(env, INTRADAY_15) == ref
    assert final["runs"] == 2 and final["windows_done"] == final["windows_planned"] == ref_requests
    assert resume_requests == ref_requests - p["windows_done"]                                          # only the unfinished windows
    assert sum(read_usage(env.settings.engine.request_ledger_path).values()) == ref_requests           # no window requested twice


def test_stop_ends_the_attempt_differently_from_pause_and_cancel_only_applies_to_unstarted_jobs(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    fake.delay = 0.25
    env = web_env
    a = env.create(intraday_spec(), start="now")["job"]
    queued = env.create(daily_spec())["job"]
    other = env.create(daily_spec(("SIMGAMMA",)))["job"]
    env.sup.tick()
    assert env.job(queued["id"])["status"] == "queued"                              # waiting behind the running job
    env.act(queued["id"], "stop", expect=409)                                       # not started: Stop does not apply
    held = env.act(queued["id"], "pause")["job"]
    assert held["status"] == "paused"                                               # a queued job can be held back
    cancelled = env.act(other["id"], "cancel")["job"]                               # a job that never started can be cancelled
    assert cancelled["status"] == "cancelled" and "before it started" in cancelled["note"]
    drive(env, lambda: env.job(a["id"])["progress"]["run_counts"].get("windows_done", 0) >= 1)
    env.act(a["id"], "cancel", expect=409)                                          # a running job cannot be cancelled
    env.act(a["id"], "stop")
    drive(env, status_is(env, a["id"], "stopped"))
    s = env.job(a["id"])
    assert s["status"] == "stopped" and set(s["actions"]) >= {"resume", "start", "abandon"}
    env.sup.tick()
    assert env.job(queued["id"])["status"] == "paused"                              # a held job is not started by the queue
    fake.delay = 0
    env.act(a["id"], "abandon", expect=400)                                          # abandoning started work needs confirmation
    out = env.act(a["id"], "abandon", confirm=True)["job"]
    assert out["status"] == "cancelled" and "downloaded data is kept" in out["note"]


def test_force_stop_is_a_hard_kill_that_loses_no_committed_work(web_env, fake, monkeypatch, tmp_path):
    point_workers_at(monkeypatch, fake)
    fake.delay = 0.3
    env = web_env
    job = env.create(intraday_spec(), start="now")["job"]
    drive(env, lambda: env.job(job["id"])["progress"]["run_counts"].get("windows_done", 0) >= 2)
    env.act(job["id"], "kill", expect=400)                                           # needs confirmation
    killed = env.act(job["id"], "kill", confirm=True)["job"]
    assert killed["status"] == "interrupted" and "Resume" in killed["next_action"]
    assert not psutil.pid_exists(killed["worker"]["pid"]) or not killed["worker"]["alive"]
    fake.delay = 0
    env.act(job["id"], "resume")
    drive(env, status_is(env, job["id"], "completed"))
    ref, _ = reference_rows(tmp_path, fake, INTRADAY_15, date(2023, 6, 1), date(2024, 6, 3), ("SIMALPHA", "SIMBETA", "SIMGAMMA"))
    assert db_rows(env, INTRADAY_15) == ref


def test_server_restart_readopts_a_live_worker_and_recovers_one_that_finished_meanwhile(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    fake.delay = 0.3
    env = web_env
    job = env.create(intraday_spec(), start="now")["job"]
    drive(env, lambda: env.job(job["id"])["progress"]["run_counts"].get("windows_done", 0) >= 1)
    pid = env.job(job["id"])["worker"]["pid"]
    env2 = env.reopen()                                    # "the server was restarted": the worker is a separate process
    try:
        assert psutil.pid_exists(pid)
        j = env2.job(job["id"])
        assert j["status"] in ("running", "preparing") and j["worker"]["alive"]          # re-adopted, not duplicated
        assert [e["type"] for e in env2.client.get(f"/api/jobs/{job['id']}/events").json()][0] == "adopted"
        assert len(env2.sup._active_jobs()) == 1
        drive(env2, status_is(env2, job["id"], "completed"), message="completion after re-adoption")
    finally:
        env2.close()
    # and a worker that finishes while the server is DOWN is finalized from its result file on the next start
    fake.delay = 0.05
    env3 = Env(web_env.tmp / "again", databases=True)
    try:
        j2 = env3.create(daily_spec(), start="now")["job"]
        pid2 = env3.job(j2["id"])["worker"]["pid"]
        psutil.Process(pid2).wait(timeout=60)
        env4 = env3.reopen()
        try:
            assert env4.job(j2["id"])["status"] == "completed"                             # learned from result.json
        finally:
            env4.close()
    finally:
        env3.close()


def test_a_worker_that_vanished_leaves_the_job_interrupted_and_resumable(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    fake.delay = 0.4
    env = web_env
    job = env.create(intraday_spec(), start="now")["job"]
    drive(env, lambda: env.job(job["id"])["progress"]["run_counts"].get("windows_done", 0) >= 1)
    pid = env.job(job["id"])["worker"]["pid"]
    psutil.Process(pid).kill()                                                       # crash / power loss
    drive(env, status_is(env, job["id"], "interrupted"))
    j = env.job(job["id"])
    assert "unexpectedly" in j["note"] or "gone" in j["note"] and "resume" in j["actions"]
    fake.delay = 0
    env.act(job["id"], "resume")
    drive(env, status_is(env, job["id"], "completed"))


def test_one_writer_per_database_duplicates_are_refused_and_independent_databases_can_overlap_only_when_allowed(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    fake.delay = 0.3
    env = web_env
    a = env.create(intraday_spec(("SIMALPHA",)), start="now")["job"]
    dup = env.client.post("/api/jobs", json={"spec": intraday_spec(("SIMALPHA",)), "start": "queue", "confirm": True})
    assert dup.status_code == 409 and "identical job" in dup.json()["error"]
    b = env.create(intraday_spec(("SIMBETA",)))["job"]                                # same database
    d = env.create(daily_spec(("SIMALPHA",)))["job"]                                  # other database
    env.sup.tick()
    assert env.job(b["id"])["status"] == "queued" and "one writer per database" in env.job(b["id"])["block_reason"]
    assert env.job(d["id"])["status"] == "queued" and "one at a time" in env.job(d["id"])["block_reason"]
    assert len(env.sup._active_jobs()) == 1
    env.client.put("/api/settings", json={"parallel_timeframes": True})
    env.sup.tick()
    assert env.job(d["id"])["status"] in ("preparing", "running", "completed")       # now the daily database may run alongside
    assert "--rps 2" in env.job(d["id"])["command"]                                   # and each worker is slowed so 2 + 2 < Dhan's 5/s
    assert env.job(b["id"])["status"] == "queued"                                     # still one writer for the 15-minute database
    fake.delay = 0
    drive(env, lambda: all(env.job(i)["status"] == "completed" for i in (a["id"], b["id"], d["id"])), timeout=120)


def test_the_request_budget_is_enforced_through_the_shared_ledger(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    env = web_env
    ledger = env.settings.engine.request_ledger_path
    from stockdata.budget import UsageLedger
    seeded = UsageLedger(ledger)
    for _ in range(79_998):
        pass
    con = seeded._con
    con.execute("INSERT INTO usage VALUES (?, 'otherapp', ?, 'x')", (seeded.day(), 79_998))
    job = env.create(intraday_spec())["job"]
    usage = env.client.get("/api/usage").json()
    assert usage["used_today"] == 79_998 and usage["remaining_day"] == 2 and usage["max_per_day"] == 80_000
    plan = env.client.post("/api/plan", json=intraday_spec()).json()
    assert plan["budget"]["fits"] is False and any(w["type"] == "budget" for w in plan["warnings"])
    drive(env, status_is(env, job["id"], "stopped_by_limit"), message="stop at the day cap")
    j = env.job(job["id"])
    assert j["exit_code"] == 4 and "budget" in j["next_action"].lower() and j["attempts_used"] == 2
    assert sum(read_usage(ledger).values()) == 80_000                                  # never exceeded the cap
    seeded.close()


def test_the_per_run_cap_stops_cleanly_and_a_resume_with_a_larger_cap_finishes_without_repeats(web_env, fake, monkeypatch, tmp_path):
    point_workers_at(monkeypatch, fake)
    env = web_env
    env.client.put("/api/settings", json={"max_per_run": 3})
    job = env.create(intraday_spec())["job"]
    drive(env, status_is(env, job["id"], "stopped_by_limit"))
    j = env.job(job["id"])
    assert j["windows_done"] == 3 and j["attempts_used"] == 3
    env.client.put("/api/settings", json={"max_per_run": 60_000})
    env.act(job["id"], "resume")
    drive(env, status_is(env, job["id"], "completed"))
    ref, ref_requests = reference_rows(tmp_path, fake, INTRADAY_15, date(2023, 6, 1), date(2024, 6, 3), ("SIMALPHA", "SIMBETA", "SIMGAMMA"))
    assert db_rows(env, INTRADAY_15) == ref
    assert sum(read_usage(env.settings.engine.request_ledger_path).values()) == ref_requests    # exactly one attempt per window overall


def test_an_expired_token_fails_the_job_with_a_clear_action_and_resume_after_fixing_it_completes(tmp_path, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    env = Env(tmp_path, token="expired-token-1234")
    try:
        job = env.create(intraday_spec(), start="now")["job"]
        drive(env, status_is(env, job["id"], "failed"))
        j = env.job(job["id"])
        assert j["exit_code"] == 3 and "token" in j["next_action"].lower() and "expired-token-1234" not in json.dumps(j)
        assert "expired-token-1234" not in env.client.get(f"/api/jobs/{job['id']}/log").text
        assert env.client.put("/api/credentials", json={"access_token": "test-token"}).status_code == 200
        env.act(job["id"], "resume")
        drive(env, status_is(env, job["id"], "completed"))
    finally:
        env.close()


def test_missing_credentials_keep_a_job_queued_with_the_reason(tmp_path, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    env = Env(tmp_path, token=None, client_id=None)
    try:
        job = env.create(daily_spec())["job"]
        env.sup.tick()
        j = env.job(job["id"])
        assert j["status"] == "queued" and "credentials" in j["block_reason"]
        plan = env.client.post("/api/plan", json=daily_spec()).json()
        assert plan["blockers"] and plan["blockers"][0]["type"] == "credentials"
        r = env.client.post("/api/jobs", json={"spec": daily_spec(("SIMGAMMA",)), "start": "now", "confirm": True})
        assert r.status_code == 409 and "credentials" in r.json()["error"]
    finally:
        env.close()


def test_failed_windows_are_retried_by_a_new_job_using_the_engines_retry_flag(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    env = web_env
    fake.fail(times=1, status=400, match=lambda p: p["securityId"] == "900001",
              body={"errorType": "Input_Exception", "errorCode": "DH-907", "errorMessage": "no data present"})
    job = env.create(daily_spec(("SIMALPHA", "SIMBETA")))["job"]
    drive(env, status_is(env, job["id"], "completed_with_warnings"))
    j = env.job(job["id"])
    assert j["exit_code"] == 2 and j["failed_windows"] == 1 and "retry_failed" in j["actions"]
    env.act(job["id"], "retry_failed", expect=400)                                   # spends requests: needs confirmation
    retry = env.act(job["id"], "retry_failed", confirm=True)["job"]
    assert retry["retry_failed"] is True and "--retry-failed" in retry["command"] and retry["source_job_id"] == job["id"]
    drive(env, status_is(env, retry["id"], "completed"))
    assert "failed" not in window_states(env, DAILY)
    # recording a reviewed DH-907 as an empty range is a separate, explicit, narrow action
    bad = env.client.post("/api/jobs", json={"spec": {**daily_spec(), "retry_failed": True, "empty_error_codes": ["DH-907"],
                                                      "selection": {"mode": "all"}, "confirm_full": True}, "confirm": True})
    assert bad.status_code == 422 and "never the whole universe" in bad.text


def test_campaign_jobs_run_one_after_another_and_a_failure_holds_the_rest(tmp_path, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    env = Env(tmp_path, token="wrong-token-1234")
    try:
        r = env.client.post("/api/campaigns", json={"name": "history", "confirm": True,
                                                    "jobs": [intraday_spec(("SIMALPHA",)), daily_spec(("SIMALPHA",))]})
        assert r.status_code == 200, r.text
        camp = r.json()
        first, second = camp["jobs"]
        drive(env, status_is(env, first["id"], "failed"))
        env.sup.tick()
        assert env.job(second["id"])["status"] == "queued" and "waiting for job" in env.job(second["id"])["block_reason"]
        assert env.client.get("/api/campaigns").json()[0]["status"] == "needs_attention"
        env.client.put("/api/credentials", json={"access_token": "test-token"})
        env.act(first["id"], "resume")
        drive(env, lambda: env.job(second["id"])["status"] == "completed", timeout=120)
        c = env.client.get("/api/campaigns").json()[0]
        assert c["status"] == "completed" and c["jobs_done"] == 2
    finally:
        env.close()


def test_queue_switch_and_reordering(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    env = web_env
    a = env.create(daily_spec(("SIMALPHA",)))["job"]
    b = env.create(daily_spec(("SIMBETA",)))["job"]
    env.client.post("/api/queue", json={"enabled": False})
    env.sup.tick()
    assert env.job(a["id"])["status"] == "queued" and "switched off" in env.job(a["id"])["block_reason"]
    order = env.client.post("/api/jobs/reorder", json={"ids": [b["id"], a["id"]]}).json()
    assert [j["id"] for j in order if j["status"] == "queued"] == [b["id"], a["id"]]
    env.client.post("/api/queue", json={"enabled": True})
    drive(env, status_is(env, b["id"], "completed", "running", "preparing"))
    assert env.job(b["id"])["started_at"] and (not env.job(a["id"])["started_at"] or env.job(b["id"])["started_at"] <= env.job(a["id"])["started_at"])
    drive(env, lambda: env.job(a["id"])["status"] == "completed")


def test_instrument_master_refresh_is_exclusive_and_uses_the_real_cli_command(web_env):
    env = web_env
    r = env.client.post("/api/instruments/refresh", json={"confirm": True}).json()
    job = r["job"]
    assert job["kind"] == "instruments_load" and job["command"].endswith("instruments load --fetch")
    assert job["status"] in ("preparing", "running", "failed", "completed")            # it started; (no network here, so it may fail)
    drive(env, lambda: env.job(job["id"])["status"] in ("failed", "completed"), timeout=120)


def _fake_worker(env, *, progress_age_seconds: int, alive: bool = True):
    """A job row that looks like a running download, backed by a sleeping process we control (not a real downloader)."""
    import subprocess
    import sys
    from datetime import datetime, timedelta, timezone
    from stockdata.web.store import now
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    created = psutil.Process(proc.pid).create_time()
    if not alive:
        proc.kill(); proc.wait()
    run_dir = env.settings.jobs_dir / "99" / "run_1"
    run_dir.mkdir(parents=True)
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=progress_age_seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    (run_dir / "progress.json").write_text(json.dumps({"pid": proc.pid, "started_at": stamp, "updated_at": stamp, "phase": "running",
        "plan": {"instruments": 3, "windows": 9}, "counts": {"windows_done": 2, "instruments_done": 0}, "requests": 2}))
    spec = {"kind": "download", "timeframe": "15min", "selection": {"mode": "all"}, "confirm_full": True}
    env.store.execute("INSERT INTO jobs (id, name, kind, timeframe, status, options, queue_pos, created_at, runs) VALUES (99, 'x', 'download', '15min', 'running', ?, 1, ?, 1)",
                      (json.dumps({"spec": spec}), now()))
    env.store.execute("INSERT INTO job_runs (job_id, seq, started_at, pid, pid_create_time, control_dir, log_path) VALUES (99, 1, ?, ?, ?, ?, ?)",
                      (now(), proc.pid, created, str(run_dir), str(run_dir / "worker.log")))
    return proc


def test_a_live_but_silent_worker_is_flagged_stalled_and_never_killed_or_replaced(web_env):
    proc = _fake_worker(web_env, progress_age_seconds=600)
    try:
        web_env.sup.tick()
        j = web_env.job(99)
        assert j["status"] == "running" and j["worker"]["alive"] is True and j["worker"]["stale"] is True
        assert j["worker"]["heartbeat_age_seconds"] >= 600
        assert len(web_env.sup._active_jobs()) == 1                                   # no second worker is started for it
    finally:
        proc.kill()


def test_a_fresh_heartbeat_is_not_stalled_and_a_dead_process_is_interrupted_not_running(web_env):
    proc = _fake_worker(web_env, progress_age_seconds=3)
    try:
        web_env.sup.tick()
        j = web_env.job(99)
        assert j["worker"]["stale"] is False and j["status"] == "running"
        assert j["progress"]["windows_done"] == 2 and j["progress"]["windows_planned"] == 9
    finally:
        proc.kill(); proc.wait()
    web_env.sup.tick()
    j = web_env.job(99)
    assert j["status"] == "interrupted" and j["worker"]["alive"] is False and "resume" in j["actions"]


def test_after_a_restart_interrupted_jobs_wait_for_you_and_are_not_resumed_automatically(web_env):
    proc = _fake_worker(web_env, progress_age_seconds=5, alive=False)
    env2 = web_env.reopen()                                                            # a restart with the worker gone
    try:
        j = env2.job(99)
        assert j["status"] == "interrupted"
        env2.sup.tick(); env2.sup.tick()
        assert env2.job(99)["status"] == "interrupted"                                 # not started behind your back
        ov = env2.client.get("/api/overview").json()
        assert [r["id"] for r in ov["resumable"]] == [99] and ov["jobs"]["interrupted"] == 1
        assert env2.act(99, "resume")["job"]["status"] == "queued"
    finally:
        env2.close()


def test_the_activity_view_reports_the_active_worker_and_its_recent_log(web_env, fake, monkeypatch):
    point_workers_at(monkeypatch, fake)
    fake.delay = 0.3
    env = web_env
    job = env.create(intraday_spec(), start="now")["job"]
    drive(env, lambda: env.job(job["id"])["progress"]["run_counts"].get("windows_done", 0) >= 2)
    a = env.client.get("/api/activity").json()
    assert a["idle_note"] is None and [j["id"] for j in a["active"]] == [job["id"]]
    w = a["active"][0]
    assert w["progress"]["current"]["instrument"]["symbol"] in ("SIMALPHA", "SIMBETA", "SIMGAMMA")
    assert w["progress"]["run_counts"]["windows_done"] >= 2 and "retries" in w["progress"]["run_counts"]
    assert "SIMALPHA" in w["log_tail"] or "15min:" in w["log_tail"]
    assert any(e["type"] == "started" for e in a["events"]) and a["usage"]["used_today"] >= 2
    env.act(job["id"], "stop"); fake.delay = 0
    drive(env, status_is(env, job["id"], "stopped"))
    assert env.client.get("/api/activity").json()["idle_note"] == "No download worker is running."


def test_a_daily_budget_stop_can_resume_by_itself_after_the_ist_day_rolls_over_when_enabled(web_env):
    env = web_env
    j = env.create(daily_spec())["job"]
    env.store.execute("UPDATE jobs SET status = 'stopped_by_limit', runs = 1, finished_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (j["id"],))
    env.sup.tick()
    assert env.job(j["id"])["status"] == "stopped_by_limit"                           # off by default: waits for you
    env.client.put("/api/settings", json={"auto_resume_budget": True, "queue_enabled": False})
    env.sup.tick()
    assert env.job(j["id"])["status"] == "queued"                                      # the budget reset, so it is queued again (queue switch is off: not started)
    assert "auto_resumed" in [e["type"] for e in env.client.get(f"/api/jobs/{j['id']}/events").json()]
