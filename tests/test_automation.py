"""Unattended updates: the configuration, the Windows Task Scheduler definitions and integration, and the runner (real worker
processes against a local synthetic Dhan; no Dhan traffic). The Windows backend is exercised through a fake process runner:
it could not be executed on Windows here, and these tests say exactly what is and is not covered."""
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import make_client, make_opts
from fake_dhan import FakeDhan, SimInstrument
from stockdata.budget import UsageLedger
from stockdata.db import CandleDB
from stockdata.downloader import Downloader
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.web import automation as A
from stockdata.web import automation_cli, lifecycle
from stockdata.web import taskscheduler as TS
from web_helpers import Env, fake_jwt, point_workers_at, wait_for  # noqa: F401

IST = ZoneInfo("Asia/Kolkata")
NS = {"t": TS.NS}
MONDAY = lambda: datetime(2026, 10, 12, 18, 15, tzinfo=IST)          # noqa: E731  (a market day, whatever day the suite runs)


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


# ------------------------------------------------------------------------------------------ configuration
def test_the_configuration_is_validated_and_survives_a_restart(tmp_path):
    env = Env(tmp_path)
    try:
        cfg = A.AutomationConfig(enabled=True, provider="dhan", timeframes=["15min", "daily"], weekdays=[4, 0, 0, 1],
                                 holidays=[date(2026, 10, 2)], post_close_time="18:30", overlap_days=10)
        assert cfg.timeframes == ["daily", "15min"] and cfg.weekdays == [0, 1, 4]            # daily always first; weekdays de-duplicated
        A.save_config(env.store, cfg)
        assert A.load_config(env.reopen().store) == cfg
        for bad in ({"post_close_time": "6pm"}, {"post_close_time": "24:00"}, {"weekdays": []}, {"weekdays": [7]}, {"overlap_days": 99},
                    {"timeframes": []}, {"universe": {"mode": "group"}}, {"provider": "nonsense"}):
            with pytest.raises(Exception):
                A.AutomationConfig(**bad)
    finally:
        env.close()


# --------------------------------------------------------------------------------------- task definition
def spec(**kw):
    base = dict(phase="post-close", time_ist="18:15", weekdays=[0, 1, 2, 3, 4], python=r"C:\Stock Data\.venv\Scripts\pythonw.exe",
                repo=r"C:\Stock Data", data_dir=r"D:\StockData\data", user="PC\\trader")
    base.update(kw)
    return TS.TaskSpec(**base)


def parse(xml: str) -> ET.Element:
    return ET.fromstring(xml.replace('encoding="UTF-16"', ""))


def test_the_task_definition_is_well_formed_ist_based_and_non_overlapping():
    root = parse(TS.build_task_xml(spec(), now=datetime(2026, 10, 10, 9, 0, tzinfo=IST)))
    start = root.find(".//t:StartBoundary", NS).text
    assert start == "2026-10-10T18:15:00+05:30"                  # the +05:30 offset keeps it synchronised across time zones: no local DST shift
    days = [d.tag.split("}")[1] for d in root.find(".//t:DaysOfWeek", NS)]
    assert days == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    st = root.find(".//t:Settings", NS)
    assert st.find("t:MultipleInstancesPolicy", NS).text == "IgnoreNew"                       # no overlapping instances
    assert st.find("t:StartWhenAvailable", NS).text == "true"                                  # a missed schedule runs when the PC is back
    assert st.find("t:RunOnlyIfNetworkAvailable", NS).text == "true"
    assert st.find("t:DisallowStartIfOnBatteries", NS).text == "false" and st.find("t:WakeToRun", NS).text == "false"
    assert root.find(".//t:LogonType", NS).text == "InteractiveToken"                          # no stored password
    ex = root.find(".//t:Exec", NS)
    assert ex.find("t:Command", NS).text.endswith("pythonw.exe") and ex.find("t:WorkingDirectory", NS).text == r"C:\Stock Data"
    assert ex.find("t:Arguments", NS).text == '-m stockdata.web --data-dir "D:\\StockData\\data" automation run --phase post-close --trigger scheduled'
    assert root.find(".//t:URI", NS).text == "\\StockDataMC\\Daily Update"


def test_the_trigger_time_does_not_depend_on_the_machines_time_zone():
    a = TS.build_task_xml(spec(), now=datetime(2026, 10, 10, 9, 0, tzinfo=IST))
    b = TS.build_task_xml(spec(), now=datetime(2026, 10, 10, 3, 30, tzinfo=ZoneInfo("UTC")))        # the same instant seen from UTC
    c = TS.build_task_xml(spec(), now=datetime(2026, 10, 9, 23, 30, tzinfo=ZoneInfo("America/New_York")))
    for xml in (a, b, c):
        assert "<StartBoundary>2026-10-10T18:15:00+05:30</StartBoundary>" in xml
    assert TS.xml_fingerprint(a) == TS.xml_fingerprint(a.replace("2026-10-10T18", "2026-09-01T18"))   # the creation date is not behaviour
    assert TS.xml_fingerprint(a) != TS.xml_fingerprint(TS.build_task_xml(spec(time_ist="18:45"), now=datetime(2026, 10, 10, 9, 0, tzinfo=IST)))


def test_logon_mode_wake_and_the_reconcile_phase_are_reflected_and_nothing_secret_can_enter():
    xml = TS.build_task_xml(spec(phase="reconcile", time_ist="08:30", run_whether_logged_on=True, wake_to_run=True))
    root = parse(xml)
    assert root.find(".//t:LogonType", NS).text == "S4U" and root.find(".//t:WakeToRun", NS).text == "true"
    assert "--phase reconcile" in xml and "\\StockDataMC\\Reconciliation" in xml
    for token in ("DHAN_ACCESS_TOKEN", "FYERS_ACCESS_TOKEN", "UPSTOX_ANALYTICS_TOKEN", "access-token", "Bearer"):
        assert token not in xml                                  # a task definition carries paths and phase names only
    for bad in (spec(time_ist="25:00"), spec(weekdays=[]), spec(phase="nonsense"), spec(data_dir='D:\\x"; calc.exe; "')):
        with pytest.raises(ValueError):
            TS.build_task_xml(bad)


def test_the_next_scheduled_instants_are_given_in_ist_on_market_days_only():
    runs = TS.next_runs("18:15", [0, 1, 2, 3, 4], now=datetime(2026, 10, 9, 19, 0, tzinfo=IST), count=3)    # Friday evening
    assert runs == ["2026-10-12T18:15+05:30", "2026-10-13T18:15+05:30", "2026-10-14T18:15+05:30"]


# --------------------------------------------------------------------------------- the Windows backend
class Proc:
    def __init__(self, out=b"", err=b"", code=0):
        self.stdout, self.stderr, self.returncode = out, err, code


def test_the_windows_backend_uses_fixed_argument_lists_and_utf16_definitions(tmp_path):
    calls, captured = [], {}

    def runner(argv):
        calls.append(argv)
        if "/XML" in argv and "/Create" in argv:
            captured["xml"] = Path(argv[argv.index("/XML") + 1]).read_bytes()
        return Proc()

    be = TS.WindowsBackend(runner)
    be.register("\\StockDataMC\\Daily Update", "<Task>ünï</Task>")
    assert calls[0][:4] == ["schtasks.exe", "/Create", "/TN", "\\StockDataMC\\Daily Update"] and calls[0][-1] == "/F"
    assert captured["xml"].startswith((b"\xff\xfe", b"\xfe\xff")) and "ünï" in captured["xml"].decode("utf-16")
    assert not Path(calls[0][calls[0].index("/XML") + 1]).exists()                              # the temporary file is removed
    be.set_enabled("\\StockDataMC\\Daily Update", False)
    be.set_enabled("\\StockDataMC\\Daily Update", True)
    be.run_now("\\StockDataMC\\Daily Update")
    be.unregister("\\StockDataMC\\Daily Update")
    assert [c[1] for c in calls[1:]] == ["/Change", "/Change", "/Run", "/Delete"] and calls[1][-1] == "/DISABLE" and calls[2][-1] == "/ENABLE"
    assert all(isinstance(a, str) for c in calls for a in c)                                  # argv lists, never a shell string
    with pytest.raises(TS.SchedulerError, match="outside the StockDataMC folder"):
        be.unregister("\\Microsoft\\Windows\\Defrag\\ScheduledDefrag")                           # it can never touch another task
    with pytest.raises(TS.SchedulerError):
        be.run_now("\\StockDataMC\\x; calc")


def test_the_windows_backend_reads_status_and_reports_failures_without_leaking_anything():
    payload = {"exists": True, "state": "Ready", "enabled": True, "last_run": "2026-10-09T12:45:03.0000000Z",
               "next_run": "2026-10-12T12:45:00.0000000Z", "last_result": 3, "missed": 1}

    def runner(argv):
        if argv[0].startswith("powershell"):
            assert "Get-ScheduledTask" in argv[-1] and "StockDataMC" in argv[-1] and "Daily Update" in argv[-1]
            return Proc(json.dumps(payload).encode())
        return Proc("<Task>x</Task>".encode("utf-16"))                                            # schtasks /Query /XML

    be = TS.WindowsBackend(runner)
    info = be.info("\\StockDataMC\\Daily Update")
    assert info["last_result"] == 3 and info["missed"] == 1 and info["xml"] == "<Task>x</Task>"
    s = TS.task_status(be, spec(), expected_xml=None)
    assert s["registered"] and s["enabled"] and s["last_result_text"].startswith("ACTION REQUIRED") and s["missed_runs"] == 1
    gone = TS.WindowsBackend(lambda argv: Proc(b'{"exists":false}'))
    assert TS.task_status(gone, spec())["registered"] is False
    failing = TS.WindowsBackend(lambda argv: Proc(b"", "ERROR: Access is denied.".encode(), 1))
    with pytest.raises(TS.SchedulerError, match="Access is denied"):
        failing.register("\\StockDataMC\\Daily Update", "<x/>")
    st = TS.task_status(failing, spec())
    assert st["problem"] and st["registered"] is False


def test_where_there_is_no_task_scheduler_the_screen_says_so_and_still_previews_the_definition():
    be = TS.UnavailableBackend()
    st = TS.task_status(be, spec())
    assert st["available"] is False and "only available on Windows" in st["problem"]
    with pytest.raises(TS.SchedulerError, match="only available on Windows"):
        be.register("\\StockDataMC\\Daily Update", "<x/>")


class FakeScheduler(TS.SchedulerBackend):
    available, name = True, "fake"

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.ran: list[str] = []

    def register(self, path, xml):
        TS._check_path(path)
        self.tasks[path] = {"xml": xml, "enabled": True}

    def unregister(self, path):
        self.tasks.pop(path, None)

    def set_enabled(self, path, enabled):
        self.tasks[path]["enabled"] = enabled

    def run_now(self, path):
        self.ran.append(path)

    def info(self, path):
        t = self.tasks.get(path)
        return None if t is None else {"exists": True, "state": "Ready" if t["enabled"] else "Disabled", "enabled": t["enabled"], "last_run": None,
                                       "next_run": None, "last_result": 267011, "missed": 0, "xml": t["xml"]}


def test_the_service_creates_updates_disables_and_removes_the_tasks_and_notices_manual_edits(tmp_path):
    env = Env(tmp_path)
    try:
        backend = FakeScheduler()
        svc = A.AutomationService(env.settings, env.store, env.sup, env.app.state.data, env.app.state.gate, backend=backend)
        A.save_config(env.store, A.AutomationConfig(enabled=True, post_close_time="18:20", reconcile_time="08:40"))
        st = svc.install()
        assert set(backend.tasks) == {"\\StockDataMC\\Daily Update", "\\StockDataMC\\Reconciliation"}
        assert all(t["registered"] and t["definition_current"] for t in st["tasks"])
        assert "18:20" in backend.tasks["\\StockDataMC\\Daily Update"]["xml"] and "08:40" in backend.tasks["\\StockDataMC\\Reconciliation"]["xml"]
        backend.tasks["\\StockDataMC\\Daily Update"]["xml"] = backend.tasks["\\StockDataMC\\Daily Update"]["xml"].replace("18:20", "03:00")
        assert next(t for t in svc.status()["tasks"] if t["phase"] == "post-close")["definition_current"] is False       # edited by hand
        svc.set_enabled(False)
        assert not any(t["enabled"] for t in backend.tasks.values())
        svc.set_enabled(True)
        A.save_config(env.store, A.AutomationConfig(enabled=True, reconcile_enabled=False))                              # reconciliation switched off ...
        svc.install()
        assert set(backend.tasks) == {"\\StockDataMC\\Daily Update"}                                                     # ... so its task is removed
        st = svc.remove()
        assert backend.tasks == {} and not any(t["registered"] for t in st["tasks"])
        assert [p["phase"] for p in svc.xml_preview()] == ["post-close"]
    finally:
        env.close()


# --------------------------------------------------------------------------------------------- the runner
def seeded(env, dhan):
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, env.settings.data_dir / tf.db_name) as db:
            Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=["SIMALPHA", "SIMBETA"]), make_opts(tf, window_days=365 if not tf.is_intraday else 89))


def runner_for(env, **kw):
    return A.AutomationRunner(env.sup, env.store, env.app.state.data, env.app.state.gate, env.settings, tick=env.sup.tick,
                              sleep=lambda s: time.sleep(0.05), **kw)


def counts(env):
    out = {}
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, env.settings.data_dir / tf.db_name, read_only=True) as db:
            out[tf.name] = db.con.execute(f"SELECT count(*) FROM {tf.table}").fetchone()[0]
    return out


@pytest.fixture
def aenv(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKDATA_RPS", "1000")
    env = Env(tmp_path)
    yield env
    env.close()


def test_a_run_updates_every_timeframe_in_order_records_everything_and_a_second_run_adds_nothing(aenv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded(aenv, dhan)
    before = counts(aenv)
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, provider="dhan"))
    res = runner_for(aenv).run(phase="post-close", trigger="manual")
    assert res.status == "completed" and res.exit_code == 0, res.summary
    after = counts(aenv)
    assert after["daily"] > before["daily"] and after["15min"] > before["15min"]
    jobs = res.summary["jobs"]
    assert [j["timeframe"] for j in jobs] == ["daily", "15min"] and all(j["provider"] == "dhan" and j["status"] == "completed" for j in jobs)
    assert jobs[0]["id"] < jobs[1]["id"] and res.summary["requests"]["dhan"] > 0 and res.summary["rows_inserted"] > 0
    rec = aenv.store.one("SELECT * FROM automation_runs ORDER BY id DESC")
    assert rec["status"] == "completed" and rec["exit_code"] == 0 and rec["finished_at"] and rec["trigger"] == "manual"
    assert all(aenv.job(j["id"])["lane"] == "live" for j in jobs)                                  # the live lane, not the historical one
    again = runner_for(aenv).run(phase="post-close", trigger="manual")
    assert again.status == "completed" and counts(aenv) == after                                       # idempotent
    with CandleDB(INTRADAY_15, aenv.settings.data_dir / INTRADAY_15.db_name, read_only=True) as db:
        assert db.con.execute("SELECT count(*) FROM candle_revisions").fetchone()[0] == 0
        assert db.con.execute("SELECT count(*) FROM (SELECT 1 FROM candles_15min GROUP BY security_id, candle_timestamp HAVING count(*) > 1)").fetchone()[0] == 0


def test_a_missed_schedule_is_caught_up_by_the_next_run_from_the_last_stored_candle(aenv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded(aenv, dhan)                                                                               # data ends 2024-06-02; "today" is far later
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, provider="dhan", timeframes=["daily"]))
    res = runner_for(aenv, clock=lambda: datetime(2026, 10, 12, 18, 15, tzinfo=IST)).run(phase="post-close", trigger="scheduled")      # a Monday
    assert res.status == "completed"
    with CandleDB(DAILY, aenv.settings.data_dir / DAILY.db_name, read_only=True) as db:
        last = db.con.execute("SELECT max(candle_date) FROM candles_daily WHERE security_id = 900001").fetchone()[0]
        first_new = db.con.execute("SELECT min(candle_date) FROM candles_daily WHERE security_id = 900001 AND candle_date > DATE '2024-06-03'").fetchone()[0]
    assert (datetime.now().date() - last).days <= 4 and first_new <= date(2024, 6, 10)                  # every missed day arrived, in one run


def test_scheduled_runs_respect_the_switch_the_weekdays_and_the_holiday_list(aenv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    saturday = lambda: datetime(2026, 10, 10, 18, 15, tzinfo=IST)                                    # noqa: E731
    monday = lambda: datetime(2026, 10, 12, 18, 15, tzinfo=IST)                                      # noqa: E731
    A.save_config(aenv.store, A.AutomationConfig(enabled=False))
    off = runner_for(aenv, clock=monday).run(phase="post-close", trigger="scheduled")
    assert off.status == "skipped" and off.exit_code == 0 and "switched off" in off.summary["notes"][0]
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, holidays=[date(2026, 10, 12)]))
    assert "holiday" in runner_for(aenv, clock=monday).run(phase="post-close", trigger="scheduled").summary["notes"][0]
    assert "not a market day" in runner_for(aenv, clock=saturday).run(phase="post-close", trigger="scheduled").summary["notes"][0]
    assert dhan.log == [] and aenv.store.all("SELECT id FROM jobs") == []                             # nothing was even created
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, reconcile_enabled=False))
    assert runner_for(aenv, clock=monday).run(phase="reconcile", trigger="scheduled").status == "skipped"


def test_a_missing_credential_stops_with_action_required_before_any_request(tmp_path, dhan, monkeypatch):
    monkeypatch.setenv("STOCKDATA_RPS", "1000")
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    env = Env(tmp_path, token=None, client_id=None)
    try:
        point_workers_at(monkeypatch, dhan)
        seeded_without_creds(env, dhan)
        A.save_config(env.store, A.AutomationConfig(enabled=True, master_refresh_days=90))
        res = runner_for(env, clock=MONDAY).run(phase="post-close", trigger="scheduled")
        assert res.status == "action_required" and res.exit_code == 3
        assert res.summary["timeframes"]["daily"]["status"] == "action_required" and "not configured" in res.summary["timeframes"]["daily"]["message"]
        assert env.store.all("SELECT id FROM jobs") == []
    finally:
        env.close()


def seeded_without_creds(env, dhan):
    seeded(env, dhan)
    dhan.log.clear()


def test_a_token_rejected_mid_run_is_action_required_not_a_blind_retry(tmp_path, dhan, monkeypatch):
    monkeypatch.setenv("STOCKDATA_RPS", "1000")
    env = Env(tmp_path, token="a-token-the-server-rejects")
    try:
        point_workers_at(monkeypatch, dhan)
        seeded_without_creds(env, dhan)
        A.save_config(env.store, A.AutomationConfig(enabled=True, timeframes=["daily"], max_retries=3, retry_delay_minutes=0))
        res = runner_for(env).run(phase="post-close", trigger="manual")
        assert res.status == "action_required" and res.exit_code == 3
        assert res.summary["jobs"][0]["status"] == "failed" and len(res.summary["jobs"]) == 1                  # one attempt, no retry rounds
        assert "a-token-the-server-rejects" not in json.dumps(res.summary)
    finally:
        env.close()


def test_an_exhausted_budget_or_low_disk_stops_safely_and_leaves_no_backlog(aenv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded(aenv, dhan)
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, provider="dhan", timeframes=["daily"]))
    ledger = UsageLedger(aenv.settings.engine.request_ledger_path)
    ledger._con.execute("INSERT INTO usage VALUES (?, 'daily', 80000, 'x')", (ledger.day(),))
    dhan.log.clear()
    res = runner_for(aenv, clock=MONDAY).run(phase="post-close", trigger="scheduled")
    assert res.status == "stopped_budget" and res.exit_code == 4 and dhan.log == []
    assert aenv.store.all("SELECT id FROM jobs WHERE status = 'queued'") == []                          # the unstartable job was withdrawn
    ledger.close()
    monkeypatch.setattr(A, "free_bytes", lambda p: 1024)
    low = runner_for(aenv, clock=MONDAY).run(phase="post-close", trigger="scheduled")
    assert low.status == "stopped_disk" and low.exit_code == 6


def test_a_failed_integrity_check_stops_the_run_before_the_next_timeframe(aenv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded(aenv, dhan)
    with CandleDB(DAILY, aenv.settings.data_dir / DAILY.db_name) as db:                                  # an UNFLAGGED weekend candle: a real integrity failure
        db.con.execute("INSERT INTO candles_daily (exchange_segment, security_id, candle_date, open, high, low, close, volume, epoch_raw, window_id, "
                       "ingested_at_utc) SELECT exchange_segment, security_id, DATE '2024-05-12', open, high, low, close, volume, epoch_raw, window_id, "
                       "ingested_at_utc FROM candles_daily LIMIT 1")
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, provider="dhan"))
    res = runner_for(aenv, clock=MONDAY).run(phase="post-close", trigger="scheduled")
    assert res.status == "integrity_failed" and res.exit_code == 7
    assert [j["timeframe"] for j in res.summary["jobs"]] == ["daily"]                                    # 15-minute never started
    assert any("weekday" in c["name"] for c in res.summary["timeframes"]["daily"]["integrity"])


def test_transient_failures_are_retried_within_the_configured_limit_and_then_reported(aenv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded(aenv, dhan)
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, provider="dhan", timeframes=["daily"], max_retries=2, retry_delay_minutes=0))
    body = {"errorType": "Input_Exception", "errorCode": "DH-905", "errorMessage": "bad request"}
    dhan.fail(times=2, status=400, match=lambda p: p["securityId"] == "900002", body=body)              # fails now and in the first retry
    ok = runner_for(aenv).run(phase="post-close", trigger="manual")
    assert ok.status == "completed" and ok.summary["timeframes"]["daily"]["rounds"] == 3 and len(ok.summary["jobs"]) == 3
    dhan.rules.clear()
    dhan.fail(times=1000, status=400, match=lambda p: p["securityId"] == "900002", body=body)           # never recovers
    bad = runner_for(aenv).run(phase="post-close", trigger="manual")
    assert bad.status == "completed_with_warnings" and bad.exit_code == 2
    assert len(bad.summary["jobs"]) == 3 and any("still failing after 2 retries" in n for n in bad.summary["notes"])


def test_a_run_adopts_an_identical_job_that_is_already_waiting_instead_of_duplicating_work(aenv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded(aenv, dhan)
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, provider="dhan", timeframes=["daily"]))
    aenv.client.post("/api/queue", json={"enabled": False})
    pre = aenv.create({"kind": "update", "timeframe": "daily", "provider": "dhan", "selection": {"mode": "all"}, "overlap_days": 7})["job"]
    aenv.client.post("/api/queue", json={"enabled": True})
    res = runner_for(aenv).run(phase="post-close", trigger="manual")
    assert res.status == "completed" and [j["id"] for j in res.summary["jobs"]] == [pre["id"]]            # the same job, finished once
    assert len(aenv.store.all("SELECT id FROM jobs WHERE kind = 'update'")) == 1


def test_a_stale_stopped_run_is_recorded_as_abandoned_and_does_not_block_the_next(aenv, dhan, monkeypatch):
    aenv.store.execute("INSERT INTO automation_runs (trigger, started_at, status, pid, summary) VALUES ('scheduled', ?, 'running', 999999, '{}')",
                       ("2026-10-09T12:45:00+00:00",))
    svc = A.AutomationService(aenv.settings, aenv.store, aenv.sup, aenv.app.state.data, aenv.app.state.gate, backend=FakeScheduler())
    st = svc.status()
    assert st["running"] is None and st["runs"][0]["status"] == "abandoned"
    assert any("interrupted" in r["problem"] for r in st["recovery"])
    assert "resumes from its checkpoints" in st["recovery"][0]["action"]


def test_missed_executions_are_listed_for_market_days_without_a_run(aenv):
    svc = A.AutomationService(aenv.settings, aenv.store, aenv.sup, aenv.app.state.data, aenv.app.state.gate, backend=FakeScheduler())
    cfg = A.AutomationConfig(enabled=True, holidays=[date(2026, 10, 2)], post_close_time="18:15")
    runs = [{"phase": "post-close", "status": "completed", "started_at": "2026-10-07T12:46:00+00:00"},       # Wed 18:16 IST
            {"phase": "post-close", "status": "completed", "started_at": "2026-10-08T12:46:00+00:00"}]       # Thu 18:16 IST
    missed = svc.missed(cfg, runs, now=datetime(2026, 10, 10, 9, 0, tzinfo=IST), days=8)                    # Saturday morning
    assert [m["date"] for m in missed] == ["2026-10-09", "2026-10-06", "2026-10-05"]                       # Fri, Tue, Mon; 2 Oct is a holiday, weekend skipped
    assert svc.missed(A.AutomationConfig(enabled=False), runs, now=datetime(2026, 10, 10, 9, 0, tzinfo=IST)) == []


def test_a_stale_master_is_refreshed_but_a_failed_refresh_does_not_stop_the_update(aenv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")                                                      # the real master must never be fetched in a test
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    seeded(aenv, dhan)
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, aenv.settings.data_dir / tf.db_name) as db:
            db.con.execute("UPDATE instrument_master_loads SET loaded_at_utc = TIMESTAMP '2020-01-01 00:00:00'")
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, provider="dhan", timeframes=["daily"], master_refresh_days=7))
    res = runner_for(aenv).run(phase="post-close", trigger="manual")
    assert res.summary["master"]["refresh_needed"] is True and res.summary["master"]["status"] == "failed"
    assert res.status == "completed" and any("could not be refreshed" in n for n in res.summary["notes"])


# -------------------------------------------------------------------------- headless entry point (real process)
def cli(env, args, **extra):
    environ = {**os.environ, "STOCKDATA_ENV_FILE": str(env.settings.env_file), "STOCKDATA_RPS": "1000", **extra}
    return subprocess.run([sys.executable, "-m", "stockdata.web", "--data-dir", str(env.settings.data_dir), *args], capture_output=True, text=True,
                          env=environ, cwd=str(Path(__file__).resolve().parents[1]), timeout=300)


def test_the_headless_command_updates_the_data_without_a_browser_or_a_web_server(tmp_path, dhan, monkeypatch):
    monkeypatch.setenv("STOCKDATA_RPS", "1000")
    env = Env(tmp_path)
    seeded(env, dhan)
    before = counts(env)
    A.save_config(env.store, A.AutomationConfig(enabled=True, provider="dhan", timeframes=["daily"]))
    env.store.close()
    r = cli(env, ["automation", "run", "--phase", "post-close", "--trigger", "manual"], DHAN_BASE_URL=dhan.base_url)     # 'scheduled' skips on a weekend
    assert r.returncode == 0, r.stdout + r.stderr
    assert "automatic run completed" in r.stdout and "daily: completed via dhan" in r.stdout
    assert counts(env)["daily"] > before["daily"]
    web = env.settings.web_dir
    assert not (web / "server.json").exists()                                    # no server was started, and nothing is left behind
    probe = lifecycle.ServerLock(web / "server.lock")
    assert probe.acquire()
    probe.release()                                                              # the data directory was released
    assert not any("uvicorn" in " ".join(p.info["cmdline"] or []) for p in __import__("psutil").process_iter(["cmdline"]) if str(env.settings.data_dir) in " ".join(p.info["cmdline"] or []))
    log = (env.settings.log_dir / "automation.log").read_text()
    assert "automatic run completed" in log and "test-token" not in log


def test_a_second_scheduled_instance_exits_at_once_while_a_run_is_in_progress(tmp_path, dhan):
    env = Env(tmp_path)
    env.store.close()
    held = lifecycle.ServerLock(env.settings.web_dir / "automation.lock")
    assert held.acquire()
    try:
        r = cli(env, ["automation", "run", "--phase", "post-close", "--trigger", "scheduled"], DHAN_BASE_URL=dhan.base_url)
        assert r.returncode == 75 and "already in progress" in r.stdout
        assert dhan.log == []
    finally:
        held.release()


def test_the_command_refuses_to_run_underneath_an_application_that_does_not_answer(tmp_path, dhan):
    env = Env(tmp_path)
    env.store.close()
    lock = lifecycle.ServerLock(env.settings.server_lock)
    assert lock.acquire()
    lifecycle.write_info(env.settings.server_info, port=1, url="http://127.0.0.1:1/", data_dir=str(env.settings.data_dir), host="127.0.0.1")
    try:
        r = cli(env, ["automation", "run", "--phase", "post-close", "--trigger", "scheduled"], DHAN_BASE_URL=dhan.base_url)
        assert r.returncode == 8 and "does not answer" in r.stdout and dhan.log == []
    finally:
        lock.release()


@pytest.fixture
def senv(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKDATA_RPS", "1000")
    env = Env(tmp_path, scheduler=True)               # the application's own scheduler thread does the ticking
    yield env
    env.close()


def test_the_manual_run_inside_the_application_uses_the_same_runner_and_refuses_overlap(senv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded(senv, dhan)
    before = counts(senv)
    A.save_config(senv.store, A.AutomationConfig(enabled=True, provider="dhan", timeframes=["daily"]))
    assert senv.client.post("/api/automation/run", json={"phase": "post-close"}).status_code == 400            # needs confirmation
    dhan.delay = 0.2
    started = senv.client.post("/api/automation/run", json={"phase": "post-close", "confirm": True})
    assert started.status_code == 200 and started.json()["started"] is True
    again = senv.client.post("/api/automation/run", json={"phase": "post-close", "confirm": True})
    assert again.status_code == 409 and "already in progress" in again.json()["error"]
    dhan.delay = 0
    done = wait_for(lambda: next((r for r in senv.client.get("/api/automation/runs").json() if r["status"] != "running"), None), timeout=120,
                    message="the in-application run")
    assert done["status"] == "completed" and done["trigger"] == "manual" and counts(senv)["daily"] > before["daily"]
    st = senv.client.get("/api/automation").json()
    assert st["last_success"]["id"] == done["id"] and st["running"] is None


def test_what_a_run_would_do_is_previewed_from_the_real_planners_without_requests(aenv, dhan, monkeypatch):
    point_workers_at(monkeypatch, dhan)
    seeded(aenv, dhan)
    dhan.log.clear()
    A.save_config(aenv.store, A.AutomationConfig(enabled=True, provider="dhan"))
    plan = aenv.client.get("/api/automation/plan").json()
    assert [t["timeframe"] for t in plan["timeframes"]] == ["daily", "15min"] and all(t["requests"] > 0 for t in plan["timeframes"])
    assert plan["timeframes"][0]["update"]["needs_backfill"] == 1 and plan["timeframes"][0]["needs_backfill"] == ["SIMGAMMA"]
    assert dhan.log == []                                                                                    # previewing sends nothing


class RefusesLoggedOff(FakeScheduler):
    """Windows answers 'Access is denied' to a task that runs while logged off, unless the account may do that (SIMULATED)."""

    def register(self, path, xml):
        if "<LogonType>S4U</LogonType>" in xml:
            raise TS.SchedulerError("could not register the scheduled task: ERROR: Access is denied.")
        super().register(path, xml)


class RefusesEverything(FakeScheduler):
    def register(self, path, xml):
        raise TS.SchedulerError("could not register the scheduled task: ERROR: Access is denied.")


def make_service(env, backend):
    return A.AutomationService(env.settings, env.store, env.sup, env.app.state.data, env.app.state.gate, backend=backend)


def test_a_refused_run_when_logged_off_task_falls_back_to_signed_in_only_and_says_so(tmp_path):
    env = Env(tmp_path)
    try:
        backend = RefusesLoggedOff()
        svc = make_service(env, backend)
        A.save_config(env.store, A.AutomationConfig(enabled=True, run_whether_logged_on=True))
        out = svc.install()
        assert set(backend.tasks) == {"\\StockDataMC\\Daily Update", "\\StockDataMC\\Reconciliation"}               # both tasks exist
        assert all("<LogonType>InteractiveToken</LogonType>" in t["xml"] and "S4U" not in t["xml"] for t in backend.tasks.values())
        assert "SIGNED IN" in out["notice"] and "Access is denied" in out["notice"] and "Run as administrator" in out["notice"]
        assert out["config"]["run_whether_logged_on"] is False                                                        # saved settings agree with the tasks
        assert all(t["registered"] and t["definition_current"] for t in out["tasks"])
        again = svc.install()                                                                                          # nothing left to explain
        assert again["notice"] is None
    finally:
        env.close()


def test_a_refusal_that_is_not_about_logging_off_gives_an_actionable_message_and_changes_no_settings(tmp_path):
    env = Env(tmp_path)
    try:
        svc = make_service(env, RefusesEverything())
        A.save_config(env.store, A.AutomationConfig(enabled=True, run_whether_logged_on=True))
        with pytest.raises(A.JobError) as e:
            svc.install()
        assert "Access is denied" in str(e.value) and "Run as administrator" in str(e.value) and "policy" in str(e.value)
        assert A.load_config(env.store).run_whether_logged_on is True                                                  # the person's choice is not rewritten
        A.save_config(env.store, A.AutomationConfig(enabled=True, run_whether_logged_on=False))
        with pytest.raises(A.JobError, match="Run as administrator"):
            svc.install()
    finally:
        env.close()


def test_the_install_endpoint_returns_the_notice(tmp_path):
    env = Env(tmp_path)
    try:
        env.app.state.automation.backend = RefusesLoggedOff()
        A.save_config(env.store, A.AutomationConfig(enabled=True, run_whether_logged_on=True))
        r = env.client.post("/api/automation/task/install", json={"confirm": True})
        assert r.status_code == 200 and "SIGNED IN" in r.json()["notice"]
    finally:
        env.close()
