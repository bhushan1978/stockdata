"""Provider management and the two workflows in the web application, with real worker processes against local synthetic
servers (Dhan, FYERS, Upstox). No provider is contacted."""
import json
import os
from datetime import date

import psutil
import pytest

from conftest import load_universe, make_client, make_opts
from fake_dhan import FakeDhan, SimInstrument
from fake_providers import FakeFyers, FakeUpstox
from stockdata.budget import UsageLedger, read_usage
from stockdata.db import CandleDB
from stockdata.downloader import Downloader
from stockdata.timeframes import DAILY, INTRADAY_15
from web_helpers import Env, TOKEN_KEY, daily_spec, fake_jwt, intraday_spec, point_workers_at, wait_for  # noqa: F401
from test_web_jobs import drive, status_is

DETAILED = "master_detailed_synthetic.csv"
FYERS_TOKEN, UPSTOX_TOKEN = "fyers-test-token", "upstox-test-token"


def sims():
    return {900001: SimInstrument(900001, listing=date(2000, 1, 3), base=2500.0),
            900002: SimInstrument(900002, listing=date(2024, 4, 10), base=480.0),
            900003: SimInstrument(900003, listing=date(2010, 5, 3), base=95.0, illiquid=True)}


@pytest.fixture
def dhan():
    s = FakeDhan(sims())
    s.base_url = s.start()
    yield s
    s.stop()


@pytest.fixture
def fyers(monkeypatch):
    s = sims()
    server = FakeFyers({f"NSE:SIM{n}-EQ": s[i] for n, i in (("ALPHA", 900001), ("BETA", 900002), ("GAMMA", 900003))})
    server.url = server.start()
    monkeypatch.setenv("FYERS_BASE_URL", server.url)
    yield server
    server.stop()


@pytest.fixture
def upstox(monkeypatch):
    s = sims()
    server = FakeUpstox({**{f"INE000SIM00{k}": s[900000 + k] for k in (1, 2, 3)}, "INE002A01018": s[900001]})
    server.url = server.start()
    monkeypatch.setenv("UPSTOX_BASE_URL", server.url)
    yield server
    server.stop()


@pytest.fixture
def penv(tmp_path, monkeypatch):
    from stockdata.web.credentials import all_provider_keys
    for k in all_provider_keys():                                      # saving a credential also sets it in this process: start clean
        os.environ.pop(k, None)
    monkeypatch.setenv("STOCKDATA_RPS", "1000")                       # tests only: no throttling of the local fakes
    env = Env(tmp_path, master=DETAILED)
    yield env
    env.close()
    for k in all_provider_keys():
        os.environ.pop(k, None)


def set_fyers(env, token=FYERS_TOKEN):
    r = env.client.put("/api/providers/fyers/credentials", json={"values": {"FYERS_APP_ID": "TEST-100", "FYERS_ACCESS_TOKEN": token}})
    assert r.status_code == 200, r.text
    return r.json()


def set_upstox(env, token=UPSTOX_TOKEN):
    r = env.client.put("/api/providers/upstox/credentials", json={"values": {"UPSTOX_ANALYTICS_TOKEN": token}})
    assert r.status_code == 200, r.text


# ----------------------------------------------------------------------------------------- management
def test_the_providers_screen_lists_all_three_with_honest_facts_and_never_a_secret(penv):
    set_fyers(penv, token="fyers-very-secret-token-0000")
    body = penv.client.get("/api/providers").json()
    names = [p["provider"] for p in body["providers"]]
    assert names == ["dhan", "fyers", "upstox"] and body["default"] == "dhan" and body["by_timeframe"] == {}
    assert "fyers-very-secret-token-0000" not in json.dumps(body)               # a credential never leaves the server
    fy = next(p for p in body["providers"] if p["provider"] == "fyers")
    assert fy["auth"]["state"] == "ready" and all(f["configured"] for f in fy["credentials"]["fields"])
    token_field = next(f for f in fy["credentials"]["fields"] if f["env_key"] == "FYERS_ACCESS_TOKEN")
    assert token_field["masked"].endswith("0000") and "secret" not in token_field["masked"]
    assert fy["unattended"]["status"] == "requires_login" and fy["capabilities"]["auth"]["interactive_login"] is True
    assert fy["capabilities"]["timeframes"]["15min"]["status"] == "documented_unverified"      # not verified, and it says so
    assert fy["capabilities"]["timeframes"]["15min"]["coverage_status"] == "documented_unverified"
    up = next(p for p in body["providers"] if p["provider"] == "upstox")
    assert up["auth"]["state"] == "not_configured" and up["unattended"]["status"] == "unverified"
    assert "terms" in up["capabilities"]["terms_note"].lower()
    assert body["policy"]["same_databases"].startswith("every provider writes into the same two databases")


def test_only_a_providers_own_fields_can_be_written_and_removal_needs_confirmation(penv):
    bad = penv.client.put("/api/providers/fyers/credentials", json={"values": {"DHAN_ACCESS_TOKEN": "sneaky-value-123456"}})
    assert bad.status_code == 400 and "not a credential of fyers" in bad.json()["error"]
    invalid = penv.client.put("/api/providers/fyers/credentials", json={"values": {"FYERS_ACCESS_TOKEN": "has spaces in it"}})
    assert invalid.status_code == 400 and "invalid format" in invalid.json()["error"]
    set_fyers(penv)
    assert penv.client.delete("/api/providers/fyers/credentials").status_code == 400
    cleared = penv.client.delete("/api/providers/fyers/credentials?confirm=true").json()
    assert cleared["auth"]["state"] == "not_configured" and "FYERS_ACCESS_TOKEN" not in penv.settings.env_file.read_text()
    assert penv.client.get("/api/providers/nonsense").status_code == 404


def test_changing_the_default_needs_confirmation_and_touches_no_data(penv, dhan):
    with CandleDB(DAILY, penv.settings.data_dir / DAILY.db_name) as db:
        Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=["SIMALPHA"]), make_opts(DAILY, window_days=365))
    before_files = sorted(os.listdir(penv.settings.data_dir))
    with CandleDB(DAILY, penv.settings.data_dir / DAILY.db_name, read_only=True) as db:
        windows = db.con.execute("SELECT * FROM download_windows ORDER BY ALL").fetchall()
        candles = db.con.execute("SELECT * FROM candles_daily ORDER BY ALL").fetchall()
    assert penv.client.put("/api/providers/defaults", json={"default": "fyers"}).status_code == 400          # no confirmation
    r = penv.client.put("/api/providers/defaults", json={"default": "fyers", "by_timeframe": {"15min": "upstox"}, "confirm": True})
    assert r.status_code == 200 and r.json()["default"] == "fyers" and r.json()["by_timeframe"] == {"15min": "upstox"}
    assert any("no checkpoint was reset" in u for u in r.json()["unchanged"])
    assert sorted(os.listdir(penv.settings.data_dir)) == before_files                         # no database was created
    with CandleDB(DAILY, penv.settings.data_dir / DAILY.db_name, read_only=True) as db:
        assert db.con.execute("SELECT * FROM download_windows ORDER BY ALL").fetchall() == windows
        assert db.con.execute("SELECT * FROM candles_daily ORDER BY ALL").fetchall() == candles    # nothing replaced
    bad = penv.client.put("/api/providers/defaults", json={"default": "nonsense", "confirm": True})
    assert bad.status_code in (400, 422)
    assert penv.client.put("/api/providers/defaults", json={"by_timeframe": {"15min": None}, "confirm": True}).json()["by_timeframe"] == {}


def test_connection_tests_count_one_request_and_report_failures_without_leaking(penv, fyers, upstox):
    assert penv.client.post("/api/providers/fyers/test", json={"confirm": True}).status_code == 400    # not configured: nothing sent
    assert penv.client.post("/api/providers/fyers/test", json={}).status_code == 400                   # not confirmed
    set_fyers(penv)
    ok = penv.client.post("/api/providers/fyers/test", json={"confirm": True}).json()
    assert ok["ok"] is True and ok["state"] == "ok" and ok["details"]["requests_used"] == 1
    set_upstox(penv)
    assert penv.client.post("/api/providers/upstox/test", json={"confirm": True}).json()["ok"] is True
    ledger = penv.settings.engine.request_ledger_path
    assert read_usage(ledger, provider="fyers") == {"fyers/test": 1} and read_usage(ledger, provider="upstox") == {"upstox/test": 1}
    set_fyers(penv, token="expired-fyers-token-9999")
    bad = penv.client.post("/api/providers/fyers/test", json={"confirm": True}).json()
    assert bad["ok"] is False and bad["state"] == "auth_failed" and "expired-fyers-token-9999" not in json.dumps(bad)
    last = penv.client.get("/api/providers/fyers").json()["last_connection"]
    assert last["state"] == "auth_failed"


def test_a_capability_probe_measures_what_the_provider_really_accepts(penv, upstox):
    set_upstox(penv)
    assert penv.client.post("/api/providers/upstox/probe", json={}).status_code == 400                  # needs confirmation
    r = penv.client.post("/api/providers/upstox/probe", json={"confirm": True, "symbol": "SIMALPHA"}).json()
    assert r["state"] in ("ok", "partial") and r["details"]["requests_used"] <= 20
    ver = r["details"]["verified"]
    assert ver["daily"]["has_data_since"] == "2000-01-01" and ver["daily"]["max_window_days_accepted"] >= 365
    assert ver["15min"]["has_data_since"] == "2022-01-01" and ver["15min"]["max_window_days_accepted"] == 28
    caps = penv.client.get("/api/providers/upstox").json()["capabilities"]
    assert caps["timeframes"]["15min"]["verified"]["has_data_since"] == "2022-01-01"
    assert caps["timeframes"]["15min"]["max_window_status"] == "probe_verified"
    assert caps["timeframes"]["15min"]["status"] == "documented_unverified"                               # the published claim stays labelled
    ledger = penv.settings.engine.request_ledger_path
    assert sum(read_usage(ledger, provider="upstox").values()) == r["details"]["requests_used"]


def test_a_probe_with_a_rejected_credential_stops_at_the_first_request(penv, fyers):
    set_fyers(penv, token="wrong-token-1234567")
    r = penv.client.post("/api/providers/fyers/probe", json={"confirm": True, "symbol": "SIMALPHA"}).json()
    assert r["state"] == "auth_failed" and r["details"]["requests_used"] == 1 and "wrong-token-1234567" not in json.dumps(r)


# ---------------------------------------------------------------------------------- jobs by provider
def fy_spec(**kw):
    return {**daily_spec(("SIMALPHA",)), "provider": "fyers", **kw}


def test_a_job_fixes_its_provider_when_created_and_a_later_default_change_does_not_move_it(penv, fyers, dhan):
    set_fyers(penv)
    penv.client.post("/api/queue", json={"enabled": False})
    assert penv.client.put("/api/providers/defaults", json={"default": "fyers", "confirm": True}).status_code == 200
    job = penv.create(daily_spec(("SIMALPHA",)))["job"]                         # no provider named: the default (FYERS) is resolved NOW
    assert job["provider"] == "fyers" and "--provider fyers" in job["command"]
    penv.client.put("/api/providers/defaults", json={"default": "dhan", "confirm": True})
    assert penv.job(job["id"])["provider"] == "fyers"                           # the queued job keeps its source
    other = penv.create(daily_spec(("SIMBETA",)))["job"]
    assert other["provider"] == "dhan" and "--provider dhan" in other["command"]


def test_each_providers_own_request_limits_go_into_its_job_and_a_historical_job_leaves_the_live_reserve(penv, fyers):
    set_fyers(penv)
    penv.client.post("/api/queue", json={"enabled": False})
    j = penv.create(fy_spec())["job"]
    assert "--max-requests-per-day 7000" in j["command"]                        # FYERS' conservative 9,000 minus the 2,000 live reserve
    d = penv.create(daily_spec(("SIMBETA",), provider="dhan"))["job"]
    assert "--max-requests-per-day 78000" in d["command"]
    up = penv.client.put("/api/providers/fyers/limits", json={"per_day": 5000, "confirm": True}).json()
    assert up["limits"]["per_day"] == 5000
    too_big = penv.client.put("/api/providers/fyers/limits", json={"per_day": 500_000, "confirm": True})
    assert too_big.status_code == 400 and "exceeds" in too_big.json()["error"]


def test_a_job_for_an_unconfigured_provider_waits_with_a_provider_specific_reason(penv, fyers):
    job = penv.create(fy_spec())["job"]
    penv.sup.tick()
    j = penv.job(job["id"])
    assert j["status"] == "queued" and "FYERS (API v3) credentials are not configured" in j["block_reason"]


def test_a_fyers_job_runs_in_a_real_worker_with_only_fyers_credentials_and_its_own_ledger_label(penv, fyers, dhan):
    set_fyers(penv)
    fyers.delay = 0.4
    job = penv.create(fy_spec(start_date="2023-06-01", end_date="2024-06-03"))["job"]
    drive(penv, status_is(penv, job["id"], "preparing", "running"), message="the worker starts")
    run = penv.sup._latest_run(job["id"])
    env = psutil.Process(run["pid"]).environ()
    assert env.get("FYERS_ACCESS_TOKEN") == FYERS_TOKEN and "DHAN_ACCESS_TOKEN" not in env and "UPSTOX_ANALYTICS_TOKEN" not in env
    fyers.delay = 0
    drive(penv, status_is(penv, job["id"], "completed"), timeout=90)
    with CandleDB(DAILY, penv.settings.data_dir / DAILY.db_name, read_only=True) as db:
        assert db.con.execute("SELECT DISTINCT provider FROM candles_daily").fetchall() == [("fyers",)]
        assert db.con.execute("SELECT DISTINCT provider FROM download_windows").fetchall() == [("fyers",)]
    labels = read_usage(penv.settings.engine.request_ledger_path, provider="fyers")
    assert set(labels) == {"fyers/daily"} and labels["fyers/daily"] >= 1
    assert read_usage(penv.settings.engine.request_ledger_path, provider="dhan") == {}      # Dhan's budget was not touched
    assert penv.client.get("/api/usage").json()["providers"]["fyers"]["used_today"] == labels["fyers/daily"]


def test_one_providers_exhausted_budget_never_blocks_another(penv, fyers, dhan):
    set_fyers(penv)
    ledger = UsageLedger(penv.settings.engine.request_ledger_path)
    ledger._con.execute("INSERT INTO usage VALUES (?, 'daily', 80000, 'x')", (ledger.day(),))     # Dhan: at its cap
    dj = penv.create(daily_spec(("SIMBETA",), provider="dhan"))["job"]
    fj = penv.create(fy_spec())["job"]
    penv.sup.tick()
    assert "budget" in penv.job(dj["id"])["block_reason"]
    drive(penv, status_is(penv, fj["id"], "completed"), timeout=90)
    ledger.close()


# ------------------------------------------------------------------------------------ the two workflows
def seeded_intraday(penv, dhan, symbols=("SIMALPHA", "SIMBETA")):
    with CandleDB(INTRADAY_15, penv.settings.data_dir / INTRADAY_15.db_name) as db:
        Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=list(symbols)), make_opts(INTRADAY_15))


def live_spec(**kw):
    return {"kind": "update", "timeframe": "15min", "selection": {"mode": "symbols", "symbols": ["SIMALPHA", "SIMBETA"]}, **kw}


def test_live_job_kinds_are_validated_and_have_their_own_lane_and_plan(penv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded_intraday(penv, dhan)
    assert penv.client.post("/api/plan", json={**live_spec(), "start_date": "2024-01-01"}).status_code == 422       # no date range for an update
    assert penv.client.post("/api/plan", json={"kind": "poll", "timeframe": "daily",
                                               "selection": {"mode": "symbols", "symbols": ["SIMALPHA"]}}).status_code == 422
    assert penv.client.post("/api/plan", json={"kind": "refresh", "timeframe": "daily",
                                               "selection": {"mode": "all"}, "start_date": "2024-01-01"}).status_code == 422
    plan = penv.client.post("/api/plan", json={**live_spec(), "selection": {"mode": "symbols", "symbols": ["SIMALPHA", "SIMBETA", "SIMGAMMA"]}}).json()
    assert plan["kind"] == "update" and plan["lane"] == "live" and plan["update"]["needs_backfill"] == 1 and plan["needs_backfill"] == ["SIMGAMMA"]
    assert plan["update"]["to_update"] == 2 and plan["windows"] > 0
    poll = penv.client.post("/api/plan", json={"kind": "poll", "timeframe": "15min", "poll_seconds": 300,
                                               "selection": {"mode": "symbols", "symbols": ["SIMALPHA", "SIMBETA"]}}).json()
    assert poll["per_cycle"] == 2 and poll["cycles"] > 50 and any("not a live stream" in w["message"] for w in poll["warnings"])
    job = penv.create(live_spec())["job"]
    assert job["lane"] == "live" and job["workflow"].startswith("Live update") and "update 15min --mode post-close" in job["command"]


def test_a_live_update_runs_through_the_real_worker_and_repeating_it_adds_nothing(penv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded_intraday(penv, dhan)
    with CandleDB(INTRADAY_15, penv.settings.data_dir / INTRADAY_15.db_name, read_only=True) as db:
        before = db.con.execute("SELECT count(*) FROM candles_15min").fetchone()[0]
    job = penv.create(live_spec())["job"]
    drive(penv, status_is(penv, job["id"], "completed", "completed_with_warnings"), timeout=120)
    with CandleDB(INTRADAY_15, penv.settings.data_dir / INTRADAY_15.db_name, read_only=True) as db:
        after = db.con.execute("SELECT count(*) FROM candles_15min").fetchone()[0]
        assert after > before
        dupes = db.con.execute("SELECT count(*) FROM (SELECT 1 FROM candles_15min GROUP BY security_id, candle_timestamp HAVING count(*) > 1)").fetchone()[0]
        assert dupes == 0
        states = db.con.execute("SELECT security_id, last_status FROM update_state WHERE mode = 'post_close' ORDER BY 1").fetchall()
        assert states == [(900001, "ok"), (900002, "ok")]
    again = penv.create(live_spec())["job"]
    drive(penv, status_is(penv, again["id"], "completed", "completed_with_warnings"), timeout=120)
    with CandleDB(INTRADAY_15, penv.settings.data_dir / INTRADAY_15.db_name, read_only=True) as db:
        assert db.con.execute("SELECT count(*) FROM candles_15min").fetchone()[0] == after
        assert db.con.execute("SELECT count(*) FROM candle_revisions").fetchone()[0] == 0


def test_a_live_update_takes_priority_over_a_historical_backfill_on_the_same_database_and_the_backfill_resumes(penv, dhan, monkeypatch):
    """One writer per DuckDB file is a hard rule, so the live lane does not wait behind a long backfill: the backfill is
    paused at its next checkpoint, the update runs, and the backfill resumes by itself."""
    point_workers_at(monkeypatch, dhan)
    seeded_intraday(penv, dhan)
    dhan.delay = 0.3
    backfill = penv.create({**intraday_spec(("SIMALPHA",)), "start_date": "2022-01-01", "end_date": "2023-06-01"})["job"]
    drive(penv, status_is(penv, backfill["id"], "running"), message="the backfill is running")
    update = penv.create(live_spec(selection={"mode": "symbols", "symbols": ["SIMALPHA"]}))["job"]
    penv.sup.tick()
    assert penv.job(backfill["id"])["status"] in ("stopping", "paused") and penv.job(backfill["id"])["yielded_to"] == update["id"]
    assert "pause at a checkpoint" in (penv.job(update["id"])["block_reason"] or "") or penv.job(update["id"])["status"] != "queued"
    dhan.delay = 0
    drive(penv, lambda: penv.job(update["id"])["status"] in ("completed", "completed_with_warnings"), timeout=120, message="the update runs")
    drive(penv, status_is(penv, backfill["id"], "completed"), timeout=180, message="the backfill resumed and finished")
    events = [e["type"] for e in penv.client.get(f"/api/jobs/{backfill['id']}/events").json()]
    assert "yielded" in events and "auto_resumed" in events
    assert penv.job(backfill["id"])["yielded_to"] is None
    with CandleDB(INTRADAY_15, penv.settings.data_dir / INTRADAY_15.db_name, read_only=True) as db:
        done = db.con.execute("SELECT status, count(*) FROM download_windows GROUP BY 1").fetchall()
        assert dict(done).get("failed", 0) == 0
        assert db.con.execute("SELECT count(*) FROM (SELECT 1 FROM candles_15min GROUP BY security_id, candle_timestamp HAVING count(*) > 1)").fetchone()[0] == 0


def test_the_live_status_reports_freshness_last_success_and_failed_instruments_without_requesting_anything(penv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded_intraday(penv, dhan)
    n_requests = len(dhan.log)
    st0 = penv.client.get("/api/live").json()
    t0 = st0["timeframes"]["15min"]
    assert st0["market"]["phase"] in ("weekend", "pre_open", "open", "closed_not_final", "closed")
    assert t0["provider"] == "dhan" and t0["database"] == "ok" and t0["instruments_with_data"] == 2 and t0["instruments_without_candles"] == 1
    assert t0["last_success_job"] is None and t0["failed"] == [] and t0["provisional"]["bars"] == 0
    assert t0["candles_by_provider"] == [{"provider": "dhan", "candles": t0["candles_by_provider"][0]["candles"]}]
    assert st0["timeframes"]["daily"]["database"] == "ok" and st0["timeframes"]["daily"]["latest_candle"] is None
    job = penv.create(live_spec())["job"]
    drive(penv, status_is(penv, job["id"], "completed", "completed_with_warnings"), timeout=120)
    with CandleDB(INTRADAY_15, penv.settings.data_dir / INTRADAY_15.db_name) as db:
        inst = db.get_instruments(symbols=["SIMBETA"])[0]
        db.set_update_state(inst, mode="post_close", provider="dhan", status="failed", error="HTTP 500 from the provider")
    t1 = penv.client.get("/api/live").json()["timeframes"]["15min"]
    assert t1["last_success_job"]["id"] == job["id"] and t1["last_job"]["id"] == job["id"] and t1["active_job"] is None
    assert [(f["symbol"], f["status"], f["consecutive_failures"]) for f in t1["failed"]] == [("SIMBETA", "failed", 1)]
    assert t1["update_state"]["post_close"]["failed"] == 1 and t1["update_state"]["post_close"]["ok"] == 1
    assert len(dhan.log) - n_requests > 0                                  # the update job itself asked the (fake) provider ...
    before = len(dhan.log)
    penv.client.get("/api/live")
    assert len(dhan.log) == before                                          # ... but looking at the status never does


def test_market_phase_is_computed_in_ist_and_does_not_know_holidays():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from stockdata.web.live_status import market_phase
    ist = ZoneInfo("Asia/Kolkata")
    assert market_phase(datetime(2026, 10, 12, 10, 0, tzinfo=ist))["phase"] == "open"
    assert market_phase(datetime(2026, 10, 12, 8, 0, tzinfo=ist))["phase"] == "pre_open"
    assert market_phase(datetime(2026, 10, 12, 16, 0, tzinfo=ist))["phase"] == "closed_not_final"
    assert market_phase(datetime(2026, 10, 12, 18, 30, tzinfo=ist))["post_close_ready"] is True
    assert market_phase(datetime(2026, 10, 10, 11, 0, tzinfo=ist))["phase"] == "weekend"
    assert market_phase(datetime(2026, 10, 12, 5, 0, tzinfo=ZoneInfo("UTC")))["phase"] == "open"             # 05:00 UTC is 10:30 IST
    assert market_phase(datetime(2026, 10, 12, 3, 0, tzinfo=ZoneInfo("UTC")))["phase"] == "pre_open"         # 03:00 UTC is 08:30 IST


def test_a_provider_secret_appears_in_no_api_response_log_audit_entry_or_diagnostics_bundle(penv, caplog):
    import logging
    secret_fyers, secret_upstox = "fyers-canary-9f3a7c21d5e8", "upstox-canary-b41e90aa77c3"
    set_fyers(penv, token=secret_fyers)
    set_upstox(penv, token=secret_upstox)
    penv.client.put("/api/automation/config", json={"enabled": True, "provider": "fyers", "timeframes": ["daily"]})
    logging.getLogger("stockdata.web").error("provider said: Authorization: Bearer %s and token=%s", secret_upstox, secret_fyers)
    surfaces = ["/api/providers", "/api/providers/fyers", "/api/providers/upstox", "/api/providers/fyers/checks", "/api/credentials", "/api/overview",
                "/api/audit", "/api/logs/server", "/api/system/diagnostics", "/api/system/findings", "/api/automation", "/api/automation/plan",
                "/api/automation/task/xml", "/api/automation/runs", "/api/activity", "/api/usage", "/api/jobs"]
    for path in surfaces:
        r = penv.client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        assert secret_fyers not in r.text and secret_upstox not in r.text, f"a secret leaked through {path}"
    xml = penv.client.get("/api/automation/task/xml").json()
    assert all("TOKEN" not in x["arguments"].upper() and "FYERS" not in x["arguments"].upper() for x in xml)       # the command line carries paths and the phase only
    assert not any(s in x["xml"] or s in x["arguments"] for x in xml for s in (secret_fyers, secret_upstox))
    for f in penv.settings.log_dir.glob("*.log*"):
        assert secret_fyers not in f.read_text(errors="replace") and secret_upstox not in f.read_text(errors="replace"), f
