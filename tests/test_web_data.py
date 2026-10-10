"""Catalog, candle queries, quality workspace, settings and the database-access policy, over real engine output."""
import csv
import dataclasses
import io
import subprocess
import sys
import textwrap
import time
from datetime import date, datetime

import pytest

from conftest import CUTOFF, make_client, make_opts
from stockdata.db import CandleDB
from stockdata.downloader import Downloader
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.web.api import create_app
from web_helpers import Env, UI, make_settings, wait_for, web_env  # noqa: F401

SYMS = ["SIMALPHA", "SIMBETA", "SIMGAMMA"]


@pytest.fixture
def data_env(tmp_path, fake):
    env = Env(tmp_path)
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, env.settings.data_dir / tf.db_name) as db:
            Downloader(db, make_client(fake)).run(db.get_instruments(symbols=SYMS), make_opts(tf))
    yield env
    env.close()


def counts(env, tf):
    with CandleDB(tf, env.settings.data_dir / tf.db_name, read_only=True) as db:
        return db.con.execute(f"SELECT count(*) FROM {tf.table}").fetchone()[0]


def test_overview_and_catalog_report_real_numbers(data_env):
    env = data_env
    o = env.client.get("/api/overview").json()
    for name, tf in (("daily", DAILY), ("15min", INTRADAY_15)):
        d = o["databases"][name]
        assert d["candles"] == counts(env, tf) > 0 and d["instruments_with_data"] == 3 and d["source"] == "live"
        assert d["file"]["size_bytes"] > 0 and d["first"] and d["last"]
    assert o["databases"]["daily"]["master_load"]["rows_in_universe"] == 3
    assert o["usage"]["max_per_day"] == 80_000 and o["usage"]["resets_at_ist"].endswith("T00:00+05:30")
    assert o["jobs"]["total"] == 0 and o["disk"]["free_bytes"] > 0 and o["health"]["status"] == "ok"
    assert "not an estimate" in o["pending_note"] and o["pending_windows"] == 0
    again = env.client.get("/api/overview").json()["databases"]["daily"]
    assert again["source"] == "cache-current"                                      # unchanged file: no recomputation
    assert "test-token" not in env.client.get("/api/overview").text


def test_candles_are_the_stored_rows_in_ist_wall_clock_with_honest_coverage(data_env):
    env = data_env
    r = env.client.get("/api/candles", params={"timeframe": "15min", "security_id": 900001, "start": "2024-05-02", "end": "2024-05-03"}).json()
    with CandleDB(INTRADAY_15, env.settings.data_dir / INTRADAY_15.db_name, read_only=True) as db:
        stored = db.con.execute("SELECT candle_timestamp, open, high, low, close, volume FROM candles_15min WHERE security_id = 900001 "
                                "AND candle_timestamp >= '2024-05-02' AND candle_timestamp < '2024-05-04' ORDER BY 1").fetchall()
    assert r["n"] == len(stored) == 50 and r["truncated"] is False
    assert r["o"] == [x[1] for x in stored] and r["v"] == [x[5] for x in stored]
    assert r["t"][0] % 86400 == 9 * 3600 + 15 * 60 and r["t"][24] % 86400 == 15 * 3600 + 15 * 60    # IST wall clock, 09:15 first bar
    assert datetime.utcfromtimestamp(r["t"][0]) == stored[0][0]
    assert "IST" in r["timezone"] and r["coverage"]["bars_total"] > 0 and r["coverage"]["outstanding"] == []
    daily = env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001, "start": "2024-05-01", "end": "2024-05-31"}).json()
    assert daily["n"] == 23 and all(t % 86400 == 0 for t in daily["t"])                              # midnight IST per day
    early = env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001, "start": "2022-01-01", "end": "2022-02-01"}).json()
    assert early["n"] == 0 and any(w["type"] == "not_downloaded_yet" for w in early["warnings"])      # nothing is invented for undownloaded dates
    last = env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001, "bars": 5}).json()
    assert last["n"] == 5 and last["t"] == sorted(last["t"])
    assert env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 123}).status_code == 404


def test_raw_table_paging_sorting_flags_and_csv_export(data_env):
    env = data_env
    p = {"timeframe": "daily", "security_id": 900001, "start": "2024-01-01", "end": "2024-06-02"}
    t = env.client.get("/api/candles/table", params={**p, "page_size": 10, "sort": "volume", "desc": True}).json()
    assert t["total"] > 10 and len(t["rows"]) == 10
    vols = [r["volume"] for r in t["rows"]]
    assert vols == sorted(vols, reverse=True)
    p2 = env.client.get("/api/candles/table", params={**p, "page_size": 10, "page": 2, "sort": "time", "desc": False}).json()
    assert p2["rows"][0]["time"] > env.client.get("/api/candles/table", params={**p, "page_size": 10, "sort": "time", "desc": False}).json()["rows"][0]["time"]
    assert {"epoch_raw", "window_id", "ingested_utc", "flags"} <= set(t["rows"][0])
    big = env.client.get("/api/candles/table", params={**p, "page_size": 5000})
    assert big.status_code == 422
    resp = env.client.get("/api/candles/export.csv", params=p)
    assert resp.status_code == 200 and "attachment" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0][:4] == ["symbol", "security_id", "timeframe", "candle_date_ist"] and len(rows) - 1 == t["total"]
    assert all(r[0] == "SIMALPHA" for r in rows[1:])


def test_instruments_search_coverage_and_the_universe_vs_downloaded_distinction(data_env, fake):
    env = data_env
    with CandleDB(DAILY, env.settings.data_dir / DAILY.db_name) as db:
        db.con.execute("INSERT INTO instruments VALUES ('NSE_EQ', 900099, 'SIMEXTRA', 'Extra Ltd', NULL, 'EQ', 'EQUITY', 'ES', 'NSE', 'E', '1', '0.05', true, '2024-01-01', '2024-01-01')")
    r = env.client.get("/api/instruments").json()
    assert r["universe_total"] == 4 and r["total"] == 4
    both = env.client.get("/api/instruments", params={"has_data": "both"}).json()
    none = env.client.get("/api/instruments", params={"has_data": "none"}).json()
    assert both["total"] == 3 and [x["symbol"] for x in none["rows"]] == ["SIMEXTRA"]            # loaded but never downloaded
    a = next(x for x in both["rows"] if x["symbol"] == "SIMALPHA")
    assert a["daily"]["candles"] > 0 and a["intraday"]["candles"] > 0 and a["intraday"]["first"] and a["daily"]["last"]
    assert [x["symbol"] for x in env.client.get("/api/instruments/search", params={"q": "simb"}).json()][:1] == ["SIMBETA"]
    assert env.client.get("/api/instruments", params={"sort": "nonsense"}).status_code == 400
    detail = env.client.get("/api/instruments/900001").json()
    assert detail["instrument"]["symbol"] == "SIMALPHA" and detail["coverage"]["daily"]["row"]["candles"] > 0


def test_while_a_worker_owns_a_database_reads_are_refused_with_an_explanation_and_cached_numbers_remain(data_env):
    env = data_env
    first = env.client.get("/api/overview").json()["databases"]["15min"]                          # warms the cache
    env.client.get("/api/instruments")
    import os
    os.utime(env.settings.data_dir / INTRADAY_15.db_name)                                          # a worker writing changes the file
    env.store.execute("INSERT INTO jobs (name, kind, timeframe, status, options, queue_pos, created_at) VALUES "
                      "('active', 'download', '15min', 'running', ?, 1, 'x')", ('{"spec": {"kind": "download", "timeframe": "15min", "selection": {"mode": "all"}, "confirm_full": true}}',))
    busy = env.client.get("/api/candles", params={"timeframe": "15min", "security_id": 900001})
    assert busy.status_code == 423
    body = busy.json()
    assert body["busy"] is True and "one writer" in body["error"] and body["timeframe"] == "15min" and body["job_id"] > 0
    assert env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001}).status_code == 200   # other database is free
    ov = env.client.get("/api/overview").json()["databases"]["15min"]
    assert ov["candles"] == first["candles"] and ov["stale"] is True and ov["source"] == "cache" and "being written" in ov["stale_reason"] and ov["writer_job_id"] > 0
    assert env.client.get("/api/quality/15min/run").status_code in (404, 405)
    assert env.client.post("/api/quality/15min/run").status_code == 423
    assert env.client.get("/api/instruments").json()["coverage"]["15min"]["stale"] is True
    r = env.client.post("/api/plan", json={"kind": "download", "timeframe": "15min", "selection": {"mode": "symbols", "symbols": ["SIMALPHA"]}})
    assert r.status_code == 423                                                                    # the planner needs the database too
    # ...but the job can still be queued behind the running one: the plan is then worked out when it starts
    spec = {"kind": "download", "timeframe": "15min", "selection": {"mode": "symbols", "symbols": ["SIMALPHA"]}}
    queued = env.client.post("/api/jobs", json={"spec": spec, "start": "queue", "confirm": True})
    assert queued.status_code == 200, queued.text
    job = queued.json()["job"]
    assert job["status"] == "queued" and job["windows_planned"] is None and queued.json()["started"] is False
    held = env.client.post("/api/jobs", json={"spec": {**spec, "retry_failed": True}, "start": "now", "confirm": True}).json()
    assert held["started"] is False and held["waiting_reason"], held                              # held back, with the reason


def test_an_external_program_holding_the_database_is_reported_not_crashed_on(data_env):
    env = data_env
    path = env.settings.data_dir / DAILY.db_name
    code = textwrap.dedent(f"""
        import sys, duckdb
        c = duckdb.connect({str(path)!r})
        print("locked", flush=True)
        sys.stdin.read()
    """)
    holder = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        r = env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001})
        assert r.status_code == 423 and r.json()["external"] is True
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    assert env.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001}).status_code == 200


def test_quality_report_separates_failures_warnings_and_expected_behaviour(data_env):
    env = data_env
    with CandleDB(INTRADAY_15, env.settings.data_dir / INTRADAY_15.db_name) as db:
        wid, ts = db.con.execute("SELECT window_id, candle_timestamp FROM candles_15min WHERE security_id = 900001 ORDER BY 2 LIMIT 1").fetchone()
        for kind in ("negative_volume", "post_close_bar", "weekend_candle"):
            db.con.execute("INSERT INTO data_anomalies VALUES ('NSE_EQ', 900001, ?, ?, 'test', ?, now())", [ts, kind, wid])
        db.con.execute("INSERT INTO download_windows VALUES (999999, 'NSE_EQ', 900002, '2023-01-01', '2023-03-01', 'failed', 3, NULL, 0, "
                       "NULL, NULL, NULL, 400, 'DH-905', 'bad', now(), now(), NULL)")
    assert env.client.post("/api/quality/15min/run").status_code == 200
    state = wait_for(lambda: (lambda s: s if not s["running"] and s["report"] else None)(env.client.get("/api/quality/15min").json()), timeout=60)
    assert state["error"] is None
    rep = state["report"]
    assert rep["integrity"]["failures"] == [] and len(rep["integrity"]["passed"]) >= 8
    kinds = {a["kind"]: a["classification"] for a in rep["anomalies"]}
    assert kinds == {"negative_volume": "warning", "post_close_bar": "expected", "weekend_candle": "expected"}
    assert rep["outstanding_total"] == 1 and rep["outstanding_windows"][0]["symbol"] == "SIMBETA"
    assert rep["outstanding_windows"][0]["error_code"] == "DH-905"
    assert any(a["symbol"] == "SIMBETA" and "failed window" in a["reasons"][0] for a in rep["attention"])
    assert rep["severity_summary"]["confirmed_failures"] == 1 and rep["severity_summary"]["warnings"] == 1
    assert "15:00" in rep["expected_note"] and "not" in rep["expected_note"]                       # the F&O 15:00 bar is documented as expected
    assert rep["bars_per_session"] and state["stale"] is False
    csv_resp = env.client.get("/api/quality/15min/coverage.csv")
    assert csv_resp.status_code == 200 and csv_resp.text.startswith("symbol,security_id")
    assert list(env.settings.reports_dir.glob("coverage_15min_*.csv"))


def test_a_15_00_last_bar_is_never_reported_as_a_defect(tmp_path, fake):
    """Sessions that end with the 15:00 bar (24 bars) after 3 August 2026 are expected for F&O stocks."""
    env = Env(tmp_path)
    try:
        with CandleDB(INTRADAY_15, env.settings.data_dir / INTRADAY_15.db_name) as db:
            Downloader(db, make_client(fake)).run(db.get_instruments(symbols=["SIMALPHA"]), make_opts(INTRADAY_15))
            db.con.execute("DELETE FROM candles_15min WHERE security_id = 900001 AND CAST(candle_timestamp AS TIME) = '15:15:00'")
        env.client.post("/api/quality/15min/run")
        state = wait_for(lambda: (lambda s: s if not s["running"] and s["report"] else None)(env.client.get("/api/quality/15min").json()), timeout=60)
        rep = state["report"]
        assert rep["integrity"]["failures"] == [] and rep["anomalies"] == []                      # no defect, no flag for a 15:00 final bar
        assert rep["bars_per_session"][0]["bars"] == 24
    finally:
        env.close()


def test_settings_are_validated_and_the_data_directory_change_is_guarded(tmp_path):
    env = Env(tmp_path)
    try:
        s = env.client.get("/api/settings").json()
        assert s["paths"]["data_dir"] == str(env.settings.data_dir) and s["paths"]["ledger"].endswith("request_ledger.sqlite")
        assert s["limits"]["max_per_run"] == 60_000 and s["limits"]["max_per_day"] == 80_000 and s["commit"] == "test"
        assert env.client.put("/api/settings", json={"max_per_day": 100_001}).status_code == 400    # never above Dhan's documented limit
        assert env.client.put("/api/settings", json={"rps": 9}).status_code == 422
        assert env.client.put("/api/settings", json={"max_per_run": 1000, "rps": 2}).json()["limits"]["max_per_run"] == 1000
        # changing the data directory: must be absolute, must warn about a new empty environment, must be confirmed
        s2 = dataclasses.replace(env.settings, repo_root=tmp_path / "repo")
        (tmp_path / "repo").mkdir()
        app = create_app(s2, start_scheduler=False)
        from fastapi.testclient import TestClient
        c = TestClient(app, headers=UI)
        target = tmp_path / "elsewhere" / "data"
        assert c.post("/api/settings/validate-data-dir", json={"path": "relative/path"}).status_code == 400
        v = c.post("/api/settings/validate-data-dir", json={"path": str(target)}).json()
        assert v["warning"] and "NEW EMPTY environment" in v["warning"] and v["databases_present"] == {"daily": False, "15min": False}
        assert c.put("/api/settings/data-dir", json={"path": str(target)}).status_code == 400            # unconfirmed
        assert c.put("/api/settings/data-dir", json={"path": str(target), "confirm": True}).status_code == 400   # new-empty not acknowledged
        ok = c.put("/api/settings/data-dir", json={"path": str(target), "confirm": True, "confirm_new_empty": True}).json()
        assert ok["restart_required"] is True and ok["current_data_dir"] == str(env.settings.data_dir)
        assert (tmp_path / "repo" / "stockdata_web.json").exists()                                      # applied only after a restart
        same = c.post("/api/settings/validate-data-dir", json={"path": str(env.settings.data_dir)}).json()
        assert same["is_current"] and same["warning"] is None and same["databases_present"] == {"daily": True, "15min": True}
        assert c.get("/api/settings").json()["pending_data_dir"] == str(target)
    finally:
        env.close()


def test_saved_groups_can_drive_a_job_and_unknown_ids_are_refused(web_env):
    env = web_env
    ids = [900001, 900002]
    g = env.client.post("/api/groups", json={"name": "pair", "security_ids": ids}).json()
    assert g["count"] == 2 and env.client.post("/api/groups", json={"name": "pair", "security_ids": ids}).status_code == 409
    assert env.client.post("/api/groups", json={"name": "bad", "security_ids": [1]}).status_code == 400
    spec = {"kind": "download", "timeframe": "daily", "selection": {"mode": "group", "group_id": g["id"]}, "start_date": "2024-01-01", "end_date": "2024-06-03"}
    plan = env.client.post("/api/plan", json=spec).json()
    assert plan["instruments"] == 2 and plan["windows"] == 2 and plan["resolved_start"] == "2024-01-01" and plan["end_exclusive"] == "2024-06-03"
    job = env.create(spec)["job"]
    assert "--security-ids 900001,900002" in job["command"] and job["selection"]["count"] == 2
    assert env.client.delete(f"/api/groups/{g['id']}").json() == {"deleted": g["id"]}


def test_plan_preview_matches_the_dry_run_and_spends_nothing(web_env, fake):
    env = web_env
    spec = {"kind": "download", "timeframe": "15min", "selection": {"mode": "all"}, "confirm_full": True,
            "start_date": "2024-01-01", "end_date": "2024-06-03"}
    p = env.client.post("/api/plan", json=spec).json()
    assert p["instruments"] == 3 and p["windows"] == 6 and fake.log == []                          # 154 days -> 2 windows x 3 instruments
    assert p["window_days"] == 89 and p["budget"]["fits"] is True and p["budget"]["max_per_run"] == 60_000
    assert p["disk"]["free_bytes"] > 0 and p["disk"]["estimated_new_bytes"] > 0 and "bytes/row" in p["disk"]["basis"]
    assert p["paths"]["database"].endswith("stockdata_15min.duckdb") and p["command"].startswith("python -m stockdata --data-dir")
    assert env.sup.counts()["total"] == 0                                                           # previewing creates nothing
    assert not (env.settings.engine.request_ledger_path).exists()                                   # and touches no ledger
    daily = env.client.post("/api/plan", json={**spec, "timeframe": "daily", "start_date": None, "end_date": None}).json()
    assert daily["resolved_start"] == "1990-01-01" and daily["windows"] == 3 and daily["window_days"] == 40_000   # one request per instrument


def test_shutdown_reports_running_workers_and_never_kills_them_silently(web_env, fake, monkeypatch):
    env = web_env
    from web_helpers import point_workers_at, intraday_spec
    from test_web_jobs import drive
    point_workers_at(monkeypatch, fake)
    fake.delay = 0.3
    job = env.create(intraday_spec(), start="now")["job"]
    drive(env, lambda: env.job(job["id"])["progress"]["run_counts"].get("windows_done", 0) >= 1)
    assert env.client.post("/api/system/shutdown", json={}).status_code == 400                     # needs confirmation
    out = env.client.post("/api/system/shutdown", json={"confirm": True}).json()
    assert [w["id"] for w in out["workers_still_running"]] == [job["id"]] and "keep running" in out["message"]
    assert env.job(job["id"])["worker"]["alive"] is True                                            # the worker was NOT touched
    out2 = env.client.post("/api/system/shutdown", json={"confirm": True, "stop_jobs": True}).json()
    drive(env, lambda: env.job(job["id"])["status"] == "paused", timeout=60) if env.sup._thread else None
