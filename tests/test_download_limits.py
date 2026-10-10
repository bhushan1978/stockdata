"""Hitting a request cap must stop the downloader safely, keep every checkpoint and allow a duplicate-free resume."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import load_universe, make_client, run, sha256_file
from stockdata.budget import RequestBudget, UsageLedger
from stockdata.db import CandleDB
from stockdata.timeframes import DAILY, INTRADAY_15

IST = ZoneInfo("Asia/Kolkata")


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def budgeted(fake, ledger, label, per_run=10_000, per_day=10_000):
    budget = RequestBudget(ledger, label, max_per_run=per_run, max_per_day=per_day)
    return make_client(fake, budget=budget), budget


def rows(db):
    t = db.tf
    return db.con.execute(f"SELECT * FROM {t.table} ORDER BY exchange_segment, security_id, {t.key_col}").fetchall()


def clean_rows(tmp_path, fake, tf):
    with CandleDB(tf, tmp_path / f"clean_{tf.db_name}") as clean:
        load_universe(clean)
        run(clean, fake, tf)
        return [r[:9] for r in rows(clean)]


def test_run_cap_stops_safely_and_the_resume_completes_without_duplication(fake, intraday_db, tmp_path):
    tf, db = INTRADAY_15, intraday_db
    ledger = UsageLedger(tmp_path / "ledger.sqlite")
    client, budget = budgeted(fake, ledger, "15min", per_run=4)
    stats, _ = run(db, fake, tf, client=client)                       # 3 instruments x 2 windows = 6 requests needed
    assert stats["status"] == "stopped_request_limit" and "per-run request limit" in stats["note"]
    assert stats["requests_made"] == 4 == len(fake.log) == budget.run_used
    assert db.con.execute("SELECT count(*) FROM download_windows").fetchone()[0] == 4      # checkpoints preserved
    assert db.con.execute("SELECT status FROM download_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0] == "stopped_request_limit"
    assert db.outstanding_failures() == []                                                 # a stop is not a failure
    done = rows(db)
    client2, _ = budgeted(fake, ledger, "15min")
    stats2, _ = run(db, fake, tf, client=client2)
    assert stats2["status"] == "completed"
    assert len(fake.log) == 6                                         # 4 + 2: no window was requested twice
    assert set(done) <= set(rows(db))
    assert [r[:9] for r in rows(db)] == clean_rows(tmp_path, fake, tf)   # identical to an uninterrupted run
    dup = db.con.execute("SELECT count(*) FROM (SELECT 1 FROM candles_15min GROUP BY exchange_segment, security_id, "
                         "candle_timestamp HAVING count(*) > 1)").fetchone()[0]
    assert dup == 0 and sum(ledger.usage().values()) == 6


def test_a_cap_reached_during_retries_leaves_that_window_unrecorded_and_resumable(fake, daily_db, tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.sqlite")
    fake.fail(times=1, status=500)                                    # the first attempt fails; its retry would be attempt 2
    client, _ = budgeted(fake, ledger, "daily", per_run=1)
    stats, _ = run(daily_db, fake, DAILY, client=client)
    assert stats["status"] == "stopped_request_limit" and stats["requests_made"] == 1
    assert daily_db.con.execute("SELECT count(*) FROM download_windows").fetchone()[0] == 0   # nothing recorded
    client2, _ = budgeted(fake, ledger, "daily")
    stats2, _ = run(daily_db, fake, DAILY, client=client2)
    assert stats2["status"] == "completed" and len(fake.log) == 1 + 6


def test_the_day_cap_is_shared_by_both_timeframes_and_resets_at_ist_midnight(fake, daily_db, intraday_db, tmp_path):
    clock = Clock(datetime(2026, 10, 10, 20, 0, tzinfo=IST))
    ledger = UsageLedger(tmp_path / "ledger.sqlite", clock=clock)
    c_daily, _ = budgeted(fake, ledger, "daily", per_day=8)
    stats_d, _ = run(daily_db, fake, DAILY, client=c_daily)
    assert stats_d["status"] == "completed" and stats_d["requests_made"] == 6
    c_intra, b_intra = budgeted(fake, ledger, "15min", per_day=8)     # a different budget object, same ledger
    stats_i, _ = run(intraday_db, fake, INTRADAY_15, client=c_intra)
    assert stats_i["status"] == "stopped_request_limit" and "per-day" in stats_i["note"]
    assert stats_i["requests_made"] == 2                              # 8 - the 6 the daily downloader already used today
    assert ledger.usage() == {"15min": 2, "daily": 6}
    # a new IST day: the counter resets and the interrupted download resumes with only the missing windows
    clock.dt = datetime(2026, 10, 11, 0, 5, tzinfo=IST)
    c_intra2, _ = budgeted(fake, ledger, "15min", per_day=8)
    n_before = len(fake.log)
    stats_i2, _ = run(intraday_db, fake, INTRADAY_15, client=c_intra2)
    assert stats_i2["status"] == "completed" and len(fake.log) - n_before == 4      # 6 total - the 2 already done
    assert ledger.usage("2026-10-11") == {"15min": 4} and ledger.usage("2026-10-10") == {"15min": 2, "daily": 6}


def test_stopping_one_timeframe_at_a_limit_never_touches_the_other_database(fake, daily_db, intraday_db, tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.sqlite")
    c, _ = budgeted(fake, ledger, "daily")
    run(daily_db, fake, DAILY, client=c)
    path = daily_db.path
    daily_db.close()
    before = sha256_file(path)
    c2, _ = budgeted(fake, ledger, "15min", per_run=2)
    stats, _ = run(intraday_db, fake, INTRADAY_15, client=c2)
    assert stats["status"] == "stopped_request_limit"
    assert sha256_file(path) == before


def test_refused_requests_are_never_sent(fake, daily_db, tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.sqlite")
    client, budget = budgeted(fake, ledger, "daily", per_run=1, per_day=1)
    run(daily_db, fake, DAILY, client=client)
    assert len(fake.log) == 1 == sum(ledger.usage().values())
    client2, _ = budgeted(fake, ledger, "daily", per_day=1)           # today's budget is spent: not a single request goes out
    stats, _ = run(daily_db, fake, DAILY, client=client2)
    assert stats["status"] == "stopped_request_limit" and stats["requests_made"] == 0 and len(fake.log) == 1
