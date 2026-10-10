"""Live / current data: the incremental post-close update and current-session polling. They are separate workflows from the
historical download: no history is ever requested, repeating either one adds nothing, and provisional (in-session) bars never
enter the canonical candle table. Synthetic servers only."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from conftest import CUTOFF, load_universe, make_client, make_opts
from fake_dhan import FakeDhan, SimInstrument, bars_for_day
from fake_providers import FakeFyers, FakeUpstox
from stockdata import live
from stockdata.budget import RequestBudget, UsageLedger
from stockdata.db import CandleDB
from stockdata.downloader import Downloader
from stockdata.providers import get_provider
from stockdata.providers.base import MultiWindowLimiter
from stockdata.timeframes import DAILY, INTRADAY_15

STUB = SimpleNamespace(rps_override=None, rps=3.0)
FAST = lambda: MultiWindowLimiter([(10_000, 1.0)], sleep=lambda s: None)       # noqa: E731
MONDAY_EVENING = datetime(2024, 6, 10, 19, 0)         # after the 18:00 finalisation: the 10th is final, cutoff = the 11th


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


def seeded(tmp_path, tf, dhan, symbols=("SIMALPHA", "SIMBETA")):
    """A database whose history was downloaded the historical way, up to the fixed CUTOFF (2024-06-03)."""
    db = CandleDB(tf, tmp_path / tf.db_name)
    load_universe(db, "master_detailed_synthetic.csv")
    Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=list(symbols)), make_opts(tf, window_days=365 if not tf.is_intraday else 89))
    return db


def do_update(db, dhan, symbols=("SIMALPHA", "SIMBETA", "SIMGAMMA"), now=MONDAY_EVENING, **kw):
    adapter = get_provider("dhan")
    opts = live.UpdateOptions(provider="dhan", **kw)
    plan = live.plan_update(db, adapter, db.get_instruments(symbols=list(symbols)), opts, now=now)
    stats = live.run_update(db, make_client(dhan), adapter, plan, opts) if plan.to_update else {"status": "nothing", "plan": plan.summary()}
    return plan, stats


def keys(db, tf):
    return db.con.execute(f"SELECT security_id, {tf.key_col} FROM {tf.table} ORDER BY 1, 2").fetchall()


# ------------------------------------------------------------------------------------- post-close
@pytest.mark.parametrize("tf", [DAILY, INTRADAY_15], ids=["daily", "15min"])
def test_an_update_extends_existing_data_from_the_last_candle_and_never_asks_for_history(tmp_path, dhan, tf):
    with seeded(tmp_path, tf, dhan) as db:
        before = keys(db, tf)
        stored_values = db.con.execute(f"SELECT * FROM {tf.table} ORDER BY ALL").fetchall()
        dhan.log.clear()
        plan, stats = do_update(db, dhan)
        assert stats["status"] == "completed", stats
        assert {i.symbol for i in plan.to_update} == {"SIMALPHA", "SIMBETA"}
        assert [i.symbol for i in plan.needs_backfill] == ["SIMGAMMA"]               # never downloaded: an update does not do history
        after = keys(db, tf)
        assert after[:len(before)] == before or set(before) <= set(after)
        assert len(after) > len(before) and pd.Timestamp(max(k for _, k in after)) >= pd.Timestamp(2024, 6, 7)       # the days up to the 10th arrived
        # nothing older than (last covered day - overlap) was requested, and SIMGAMMA never
        earliest_asked = min(datetime.fromisoformat(r["payload"]["fromDate"].replace(" ", "T")) for r in dhan.log)
        assert earliest_asked >= CUTOFF - timedelta(days=live.config.DEFAULT_OVERLAP_DAYS + 1)
        assert all(r["payload"]["securityId"] != "900003" for r in dhan.log)
        assert db.con.execute(f"SELECT count(*) FROM {tf.table} WHERE security_id = 900003").fetchone()[0] == 0
        old_rows = {r for r in stored_values}
        assert old_rows <= set(db.con.execute(f"SELECT * FROM {tf.table}").fetchall())              # previously stored candles untouched


@pytest.mark.parametrize("tf", [DAILY, INTRADAY_15], ids=["daily", "15min"])
def test_repeating_an_update_creates_no_duplicates_and_changes_nothing(tmp_path, dhan, tf):
    with seeded(tmp_path, tf, dhan) as db:
        do_update(db, dhan)
        snapshot = db.con.execute(f"SELECT * FROM {tf.table} ORDER BY ALL").fetchall()
        revisions = db.con.execute("SELECT count(*) FROM candle_revisions").fetchone()[0]
        plan, stats = do_update(db, dhan)                                                         # again, moments later
        assert stats["status"] == "completed"
        assert db.con.execute(f"SELECT * FROM {tf.table} ORDER BY ALL").fetchall() == snapshot
        assert db.con.execute("SELECT count(*) FROM candle_revisions").fetchone()[0] == revisions == 0
        k = tf.key_col
        assert db.con.execute(f"SELECT count(*) FROM (SELECT 1 FROM {tf.table} GROUP BY security_id, {k} HAVING count(*) > 1)").fetchone()[0] == 0
        from stockdata.reports import integrity_checks
        assert [c for c in integrity_checks(db) if not c.ok] == []                                 # the canonical invariants still hold


def test_a_late_revision_inside_the_overlap_is_recorded_and_never_overwrites(tmp_path, dhan):
    with seeded(tmp_path, DAILY, dhan) as db:
        stored = db.con.execute("SELECT * FROM candles_daily ORDER BY ALL").fetchall()
        dhan.instruments[900001].base *= 1.2                       # the source now serves different values for the overlap days
        plan, stats = do_update(db, dhan, symbols=("SIMALPHA",))
        assert stats["rows_revised"] > 0 and db.con.execute("SELECT count(*) FROM candle_revisions").fetchone()[0] > 0
        assert set(stored) <= set(db.con.execute("SELECT * FROM candles_daily").fetchall())        # first-seen values are still there


def test_the_update_state_tracks_success_failures_and_skips_a_failing_instrument_until_retried(tmp_path, dhan):
    with seeded(tmp_path, DAILY, dhan) as db:
        dhan.fail(times=1000, status=400, match=lambda p: p["securityId"] == "900002",
                  body={"errorType": "Input_Exception", "errorCode": "DH-905", "errorMessage": "bad request"})
        for _ in range(live.MAX_CONSECUTIVE_FAILURES):
            do_update(db, dhan, symbols=("SIMALPHA", "SIMBETA"))
        rows = {r[0]: r[1:] for r in db.con.execute(
            "SELECT security_id, last_status, consecutive_failures, last_error FROM update_state WHERE mode = 'post_close'").fetchall()}
        assert rows[900001][0] == "ok" and rows[900001][1] == 0
        assert rows[900002][0] == "failed" and rows[900002][1] == live.MAX_CONSECUTIVE_FAILURES and "DH-905" in rows[900002][2]
        plan, _ = do_update(db, dhan, symbols=("SIMALPHA", "SIMBETA"))
        assert [i.symbol for i in plan.skipped_failing] == ["SIMBETA"]                              # not requested again every day
        dhan.rules.clear()
        plan, stats = do_update(db, dhan, symbols=("SIMALPHA", "SIMBETA"), retry_failed=True)       # the explicit Retry Failed
        assert [i.symbol for i in plan.to_update] == ["SIMALPHA", "SIMBETA"] and stats["status"] == "completed"
        assert db.con.execute("SELECT consecutive_failures FROM update_state WHERE security_id = 900002 AND mode = 'post_close'").fetchone()[0] == 0


def test_an_instrument_updated_a_moment_ago_is_skipped_when_a_minimum_interval_is_set(tmp_path, dhan):
    with seeded(tmp_path, DAILY, dhan) as db:
        do_update(db, dhan, symbols=("SIMALPHA",))
        plan, _ = do_update(db, dhan, symbols=("SIMALPHA",), min_interval_minutes=30)
        assert [i.symbol for i in plan.recent] == ["SIMALPHA"] and plan.requests == 0


def test_a_seed_window_is_an_explicit_opt_in_and_is_short(tmp_path, dhan):
    with seeded(tmp_path, DAILY, dhan) as db:
        plan, stats = do_update(db, dhan, symbols=("SIMGAMMA",), seed_days=10)
        assert [i.symbol for i in plan.to_update] == ["SIMGAMMA"] and stats["status"] == "completed"
        first = db.con.execute("SELECT min(candle_date) FROM candles_daily WHERE security_id = 900003").fetchone()[0]
        assert first >= date(2024, 6, 1)


def test_an_update_through_another_provider_is_labelled_and_never_replaces_dhans_candles(tmp_path, dhan):
    s = sims()
    fy = FakeFyers({"NSE:SIMALPHA-EQ": s[900001], "NSE:SIMBETA-EQ": s[900002]})
    fy.url = fy.start()
    try:
        with seeded(tmp_path, DAILY, dhan) as db:
            stored = db.con.execute("SELECT * FROM candles_daily ORDER BY ALL").fetchall()
            adapter = get_provider("fyers")
            opts = live.UpdateOptions(provider="fyers")
            plan = live.plan_update(db, adapter, db.get_instruments(symbols=["SIMALPHA", "SIMBETA"]), opts, now=MONDAY_EVENING)
            client = adapter.make_client({"FYERS_APP_ID": "TEST-100", "FYERS_ACCESS_TOKEN": "fyers-test-token", "FYERS_BASE_URL": fy.url},
                                         settings=STUB, label="daily", limiter=FAST())
            stats = live.run_update(db, client, adapter, plan, opts)
            assert stats["status"] == "completed"
            assert set(stored) <= set(db.con.execute("SELECT * FROM candles_daily").fetchall())   # Dhan's candles untouched
            provs = dict(db.con.execute("SELECT coalesce(provider, 'dhan'), count(*) FROM candles_daily GROUP BY 1").fetchall())
            assert provs["dhan"] == len(stored) and provs["fyers"] > 0                             # new candles carry FYERS, old stay Dhan
    finally:
        fy.stop()


# ------------------------------------------------------------------------------------ current session
class Clock:
    def __init__(self, start): self.now = start
    def __call__(self): return self.now
    def sleep(self, s): self.now += timedelta(seconds=s)


def intraday_db(tmp_path, dhan):
    db = CandleDB(INTRADAY_15, tmp_path / INTRADAY_15.db_name)
    load_universe(db, "master_detailed_synthetic.csv")
    return db


def poll(db, dhan, clock, symbols=("SIMALPHA", "SIMBETA"), control=None, **kw):
    adapter = get_provider("dhan")
    popts = live.PollOptions(provider="dhan", **{"poll_seconds": 300, **kw})
    return live.poll_current(db, make_client(dhan), adapter, db.get_instruments(symbols=list(symbols)), popts, control=control,
                             clock=clock, sleep=clock.sleep)


def test_polling_stores_only_completed_bars_as_provisional_and_never_touches_the_canonical_table(tmp_path, dhan):
    with intraday_db(tmp_path, dhan) as db:
        clock = Clock(datetime(2024, 6, 10, 11, 7))                    # a Monday, mid-session
        stats = poll(db, dhan, clock, until_close=False, max_cycles=1)
        assert stats["status"] == "completed" and stats["cycles"] == 1 and stats["failed"] == 0
        for sec in (900001, 900002):
            last = db.con.execute("SELECT max(candle_timestamp) FROM provisional_candles WHERE security_id = ?", [sec]).fetchone()[0]
            if sec == 900001:
                assert last == datetime(2024, 6, 10, 10, 45)           # the bar starting 10:45 ended 11:00 <= 11:06; 11:00 is forming
        n = db.con.execute("SELECT count(*) FROM provisional_candles WHERE security_id = 900001").fetchone()[0]
        assert n == 7                                                  # 09:15 .. 10:45
        assert db.con.execute("SELECT count(*) FROM candles_15min").fetchone()[0] == 0          # canonical stays final-only
        assert db.con.execute("SELECT count(*) FROM download_windows").fetchone()[0] == 0      # and no window was marked done


def test_polling_again_later_extends_and_overwrites_provisional_rows_without_duplicates(tmp_path, dhan):
    with intraday_db(tmp_path, dhan) as db:
        poll(db, dhan, Clock(datetime(2024, 6, 10, 11, 7)), max_cycles=1)
        poll(db, dhan, Clock(datetime(2024, 6, 10, 12, 22)), max_cycles=1)
        n = db.con.execute("SELECT count(*) FROM provisional_candles WHERE security_id = 900001").fetchone()[0]
        d = db.con.execute("SELECT count(*) FROM (SELECT 1 FROM provisional_candles GROUP BY security_id, candle_timestamp HAVING count(*) > 1)").fetchone()[0]
        assert n == 12 and d == 0                                      # 09:15 .. 12:00 inclusive-start bars, once each


def test_the_poll_loops_until_told_to_stop_and_counts_every_request(tmp_path, dhan):
    with intraday_db(tmp_path, dhan) as db:
        clock = Clock(datetime(2024, 6, 10, 10, 0))
        stats = poll(db, dhan, clock, poll_seconds=300, max_cycles=3)
        assert stats["cycles"] == 3 and stats["requests_made"] == 3 * 2 == len(dhan.log)
        assert clock.now >= datetime(2024, 6, 10, 10, 10)             # it waited between polls


def test_a_stop_request_ends_the_poll_between_instruments(tmp_path, dhan):
    with intraday_db(tmp_path, dhan) as db:
        asked = []
        control = SimpleNamespace(should_stop=lambda: len(dhan.log) >= 1, report=lambda *a, **k: asked.append(a))
        stats = poll(db, dhan, Clock(datetime(2024, 6, 10, 10, 0)), control=control)
        assert stats["status"] == "stopped_by_user" and len(dhan.log) == 1


def test_polling_refuses_a_daily_timeframe_a_short_interval_and_a_weekend(tmp_path, dhan):
    with seeded(tmp_path, DAILY, dhan) as ddb:
        with pytest.raises(ValueError, match="intraday"):
            live.poll_current(ddb, make_client(dhan), get_provider("dhan"), [], live.PollOptions(provider="dhan"))
    with intraday_db(tmp_path, dhan) as db:
        with pytest.raises(ValueError, match="at least 60"):
            poll(db, dhan, Clock(datetime(2024, 6, 10, 10, 0)), poll_seconds=30)
        dhan.log.clear()
        stats = poll(db, dhan, Clock(datetime(2024, 6, 8, 10, 0)))        # a Saturday
        assert stats["status"] == "market_closed" and dhan.log == []


def test_a_rejected_credential_ends_the_poll_and_stores_nothing(tmp_path, dhan):
    with intraday_db(tmp_path, dhan) as db:
        adapter = get_provider("dhan")
        stats = live.poll_current(db, make_client(dhan, token="wrong"), adapter, db.get_instruments(symbols=["SIMALPHA"]),
                                  live.PollOptions(provider="dhan"), clock=Clock(datetime(2024, 6, 10, 10, 0)), sleep=lambda s: None)
        assert stats["status"] == "aborted" and "authentication rejected" in stats["note"]
        assert db.con.execute("SELECT count(*) FROM provisional_candles").fetchone()[0] == 0


def test_the_post_close_update_commits_final_candles_and_retires_the_provisional_ones(tmp_path, dhan):
    with intraday_db(tmp_path, dhan) as db:
        # an earlier historical download covers up to the 3rd; the session of the 10th is polled, then finalised after the close
        Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=["SIMALPHA"]), make_opts(INTRADAY_15, window_days=89))
        poll(db, dhan, Clock(datetime(2024, 6, 10, 11, 7)), symbols=("SIMALPHA",), max_cycles=1)
        assert db.con.execute("SELECT count(*) FROM provisional_candles").fetchone()[0] == 7
        plan, stats = do_update(db, dhan, symbols=("SIMALPHA",))
        assert stats["status"] == "completed" and stats["finalized_provisional"] == 7
        assert db.con.execute("SELECT count(*) FROM provisional_candles").fetchone()[0] == 0
        final = db.con.execute("SELECT count(*) FROM candles_15min WHERE candle_timestamp >= '2024-06-10' AND candle_timestamp < '2024-06-11'").fetchone()[0]
        assert final == len(bars_for_day(dhan.instruments[900001], date(2024, 6, 10)))         # the whole final session, once
