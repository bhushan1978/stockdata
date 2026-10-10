"""The optional run control used by the web application: graceful stop between windows, progress snapshots, and a
preview (plan) that cannot drift from what a real run requests. Everything is off unless explicitly switched on."""
import json

import pytest

from conftest import load_universe, make_client, make_opts
from stockdata import jobcontrol
from stockdata.cli import main
from stockdata.config import Settings
from stockdata.db import CandleDB
from stockdata.downloader import Downloader
from stockdata.plan import build_download_plan
from stockdata.timeframes import DAILY, INTRADAY_15

from test_cli import MASTER  # noqa: E402


@pytest.fixture(params=["daily", "15min"])
def env(request, tmp_path, fake):
    tf = DAILY if request.param == "daily" else INTRADAY_15
    with CandleDB(tf, tmp_path / tf.db_name) as db:
        load_universe(db)
        yield tf, db, fake, tmp_path


def rows(db):
    return db.con.execute(f"SELECT security_id, {db.tf.key_col}, open, high, low, close, volume FROM {db.tf.table} ORDER BY 1, 2").fetchall()


def test_stop_request_ends_the_run_after_the_in_flight_window_and_a_rerun_resumes_exactly(env, monkeypatch):
    tf, db, fake, tmp = env
    ctl = jobcontrol.JobControl(tmp / "ctl")
    insts = db.get_instruments(symbols=["SIMALPHA", "SIMBETA", "SIMGAMMA"])
    opts = make_opts(tf, window_days=60 if not tf.is_intraday else tf.default_window_days)
    if not tf.is_intraday:
        opts.window_days = 60                      # several windows so there is something to stop between
    real, calls = CandleDB.commit_window, {"n": 0}

    def stopping(self, *a, **kw):
        out = real(self, *a, **kw)
        calls["n"] += 1
        if calls["n"] == 3:
            ctl.request_stop("pause")              # arrives while a window has just been committed
        return out

    monkeypatch.setattr(CandleDB, "commit_window", stopping)
    stats = Downloader(db, make_client(fake), control=ctl).run(insts, opts)
    assert stats["status"] == "stopped_by_user" and "stop requested" in stats["note"]
    assert calls["n"] == 3                                                       # nothing started after the request
    assert len(fake.log) == 3                                                    # no request was wasted or abandoned
    assert db.con.execute("SELECT count(*) FROM download_windows WHERE status IN ('failed','retriable')").fetchone()[0] == 0
    assert db.con.execute("SELECT status FROM download_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0] == "stopped_by_user"
    monkeypatch.setattr(CandleDB, "commit_window", real)
    ctl.stop_path.unlink()
    n = len(fake.log)
    resumed = Downloader(db, make_client(fake), control=ctl).run(insts, opts)
    assert resumed["status"] == "completed"
    resumed_requests = len(fake.log) - n
    clean = tmp / "clean"
    clean.mkdir()
    with CandleDB(tf, clean / tf.db_name) as other:
        load_universe(other)
        before = len(fake.log)
        Downloader(other, make_client(fake)).run(other.get_instruments(symbols=["SIMALPHA", "SIMBETA", "SIMGAMMA"]), opts)
        assert rows(other) == rows(db)                                           # identical to an uninterrupted run
        assert resumed_requests == (len(fake.log) - before) - 3                 # only the unfinished windows were requested
    dup = db.con.execute(f"SELECT count(*) FROM (SELECT 1 FROM {tf.table} GROUP BY exchange_segment, security_id, {tf.key_col} HAVING count(*) > 1)").fetchone()[0]
    assert dup == 0


def test_progress_snapshot_reports_real_counters(env):
    tf, db, fake, tmp = env
    ctl = jobcontrol.JobControl(tmp / "ctl")
    insts = db.get_instruments(symbols=["SIMALPHA", "SIMBETA"])
    ctl.report("plan", instruments=2, windows=99)
    client = make_client(fake)
    stats = Downloader(db, client, control=ctl).run(insts, make_opts(tf))
    snap = jobcontrol.read_progress(tmp / "ctl")
    assert snap["phase"] == "finished" and snap["plan"] == {"instruments": 2, "windows": 99}
    c = snap["counts"]
    assert c["windows_done"] == stats["attempted"] and c["instruments_done"] == 2
    assert c["rows_inserted"] == stats["rows_inserted"] and snap["requests"] == client.requests_sent


def test_without_control_nothing_is_written_and_behaviour_is_unchanged(env, monkeypatch):
    tf, db, fake, tmp = env
    monkeypatch.delenv(jobcontrol.ENV_VAR, raising=False)
    assert jobcontrol.JobControl.from_env() is None
    stats = Downloader(db, make_client(fake)).run(db.get_instruments(symbols=["SIMALPHA"]), make_opts(tf))
    assert stats["status"] == "completed" and not (tmp / "ctl").exists()


def test_preview_plan_equals_what_a_real_run_requests(env):
    tf, db, fake, tmp = env
    kw = dict(settings=Settings(), select_all=True, start_date=make_opts(tf).history_start.date(), end_date=make_opts(tf).cutoff.date())
    plan = build_download_plan(db, **kw)
    assert plan.windows_computed and plan.requests > 0 and len(plan.instruments) == 3
    stats = Downloader(db, make_client(fake)).run(plan.instruments, plan.opts)
    assert stats["status"] == "completed" and len(fake.log) == plan.requests
    again = build_download_plan(db, **kw)
    assert again.requests == 0 and again.instruments_with_work == 0                # fully covered: nothing to request


def test_plan_skips_known_failed_windows_unless_retry_failed(env):
    tf, db, fake, tmp = env
    fake.fail(times=1, status=400, match=lambda p: p["securityId"] == "900001",
              body={"errorType": "Input_Exception", "errorCode": "DH-905", "errorMessage": "bad"})
    kw = dict(settings=Settings(), select_all=True, start_date=make_opts(tf).history_start.date(), end_date=make_opts(tf).cutoff.date())
    plan = build_download_plan(db, **kw)
    Downloader(db, make_client(fake)).run(plan.instruments, plan.opts)
    after = build_download_plan(db, **kw)
    assert after.skipped_failed == 1 and after.requests == 0
    assert build_download_plan(db, retry_failed=True, **kw).requests == 1


def test_cli_run_with_control_dir_writes_progress_and_honours_a_stop_file_with_exit_code_5(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("DHAN_BASE_URL", fake.base_url)
    monkeypatch.setenv("STOCKDATA_RPS", "1000")
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    data, ctl = tmp_path / "data", tmp_path / "ctl"
    assert main(["--data-dir", str(data), "instruments", "load", "--file", MASTER]) == 0
    args = ["--data-dir", str(data), "download", "15min", "--all", "--confirm-full-download", "--start-date", "2024-01-01",
            "--end-date", "2024-06-03"]
    jobcontrol.forget_instances()
    monkeypatch.setenv(jobcontrol.ENV_VAR, str(ctl))
    c = jobcontrol.JobControl.from_env()
    c.request_stop("stop")                                                       # asked to stop before the first window
    monkeypatch.setenv(jobcontrol.ENV_VAR, str(ctl))
    assert main(args) == 5 and fake.log == []
    snap = jobcontrol.read_progress(ctl)
    assert snap["plan"]["windows"] == 6 and snap["phase"] == "finished"          # the plan size is known before any request
    c.stop_path.unlink()
    jobcontrol.forget_instances()                                                # a new process would start with fresh counters
    assert main(args) == 0 and len(fake.log) == 6
    assert jobcontrol.read_progress(ctl)["counts"]["windows_done"] == 6
