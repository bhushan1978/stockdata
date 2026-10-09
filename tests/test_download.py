"""Downloader mechanics against the synthetic local server.

These prove the pipeline's behaviour (boundaries, timestamps, idempotency,
retry, resume, atomicity). They say nothing about real Dhan data.
"""
from datetime import date, datetime, timedelta

import pytest

from conftest import CUTOFF, DAILY_START, INTRADAY_START, load_universe, make_client, make_opts, run
from fake_dhan import IST, bars_for_day, daily_for_day
from stockdata.db import CandleDB
from stockdata.downloader import Downloader, select_instruments
from stockdata.timeframes import DAILY, INTRADAY_15

SYMS = {"SIMALPHA": 900001, "SIMBETA": 900002, "SIMGAMMA": 900003}


@pytest.fixture(params=["daily", "15min"])
def env(request, tmp_path, fake):
    tf = DAILY if request.param == "daily" else INTRADAY_15
    with CandleDB(tf, tmp_path / tf.db_name) as db:
        load_universe(db)
        yield tf, db, fake


def expected(sim, tf, start=None, end=CUTOFF):
    start = start or (INTRADAY_START if tf.is_intraday else DAILY_START)
    out, d = [], start.date()
    while d < end.date():
        if tf.is_intraday:
            out += [(b["ts"].replace(tzinfo=None), b) for b in bars_for_day(sim, d)]
        else:
            c = daily_for_day(sim, d)
            if c:
                out.append((datetime.combine(d, datetime.min.time()), c))
        d += timedelta(days=1)
    return out


def rows(db, sec=None):
    tf = db.tf
    q = f"SELECT security_id, {tf.key_col}, open, high, low, close, volume, epoch_raw FROM {tf.table}"
    q += f" WHERE security_id = {sec}" if sec else ""
    return db.con.execute(q + f" ORDER BY security_id, {tf.key_col}").fetchall()


def n_rows(db):
    return db.con.execute(f"SELECT count(*) FROM {db.tf.table}").fetchone()[0]


def window_states(db):
    return dict(db.con.execute("SELECT status, count(*) FROM download_windows GROUP BY 1").fetchall())


# ---------------------------------------------------------------- correctness
def test_rows_match_source_and_boundaries(env):
    tf, db, fake = env
    stats, client = run(db, fake, tf)
    assert stats["status"] == "completed"
    for name, sec in SYMS.items():
        exp = expected(fake.instruments[sec], tf)
        got = rows(db, sec)
        assert len(got) == len(exp) and len(got) > 0
        assert [r[1] if tf.is_intraday else datetime.combine(r[1], datetime.min.time()) for r in got] \
            == [e[0] for e in exp]
        for r, (_, e) in zip(got, exp):
            assert (r[2], r[3], r[4], r[5], r[6]) == (e["open"], e["high"], e["low"], e["close"], e["volume"])
    start = INTRADAY_START if tf.is_intraday else DAILY_START
    first = db.con.execute(f"SELECT min({tf.key_col}) FROM {tf.table}").fetchone()[0]
    last = db.con.execute(f"SELECT max({tf.key_col}) FROM {tf.table}").fetchone()[0]
    as_dt = lambda v: v if isinstance(v, datetime) else datetime.combine(v, datetime.min.time())
    assert as_dt(first) >= start and as_dt(last) < CUTOFF       # exclusive upper boundary respected


def test_request_boundaries_endpoints_and_window_sizes(env):
    tf, db, fake = env
    run(db, fake, tf)
    reqs = fake.requests()
    assert reqs and all(r["path"].endswith(tf.endpoint) for r in reqs)
    parse = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S" if " " in s else "%Y-%m-%d")
    for r in reqs:
        p = r["payload"]
        assert parse(p["toDate"]) <= CUTOFF
        assert parse(p["fromDate"]) >= (INTRADAY_START if tf.is_intraday else DAILY_START)
        span = parse(p["toDate"]) - parse(p["fromDate"])
        if tf.is_intraday:
            assert p["interval"] == 15 and span <= timedelta(days=90)
        else:
            assert "interval" not in p
    # each instrument's windows tile its range with no overlap and no gap on a first run
    for sec in SYMS.values():
        ws = db.con.execute("SELECT window_from, window_to FROM download_windows WHERE security_id = ? "
                            "ORDER BY window_from", [sec]).fetchall()
        assert all(a[1] == b[0] for a, b in zip(ws, ws[1:]))
        assert ws[-1][1] == CUTOFF


def test_timestamps_are_ist_and_epoch_round_trips(env):
    tf, db, fake = env
    run(db, fake, tf)
    for _, key, *_rest, epoch in rows(db):
        if tf.is_intraday:
            assert key.replace(tzinfo=IST).timestamp() == epoch
            assert (key.hour, key.minute) >= (9, 15) and (key.hour, key.minute) <= (15, 15)
            assert key.minute % 15 == 0 and key.second == 0
        else:
            assert datetime.combine(key, datetime.min.time(), tzinfo=IST).timestamp() == epoch


def test_ohlc_and_volume_integrity_and_unique_keys(env):
    tf, db, fake = env
    run(db, fake, tf)
    c = db.con
    assert c.execute(f"SELECT count(*) FROM {tf.table} WHERE low > LEAST(open, close) OR high < GREATEST(open, close)"
                     ).fetchone()[0] == 0
    assert c.execute(f"SELECT count(*) FROM {tf.table} WHERE volume < 0 OR open <= 0").fetchone()[0] == 0
    dup = c.execute(f"SELECT count(*) FROM (SELECT 1 FROM {tf.table} GROUP BY exchange_segment, security_id, "
                    f"{tf.key_col} HAVING count(*) > 1)").fetchone()[0]
    assert dup == 0
    with pytest.raises(Exception, match="(?i)constraint|duplicate"):   # the key is enforced by the schema
        c.execute(f"INSERT INTO {tf.table} SELECT * FROM {tf.table} LIMIT 1")
    assert c.execute("SELECT count(*) FROM data_anomalies").fetchone()[0] == 0


def test_zero_volume_candles_are_kept_and_counted(env):
    tf, db, fake = env
    run(db, fake, tf)
    exp_zero = sum(1 for sec in SYMS.values() for _, e in expected(fake.instruments[sec], tf) if e["volume"] == 0)
    got_zero = db.con.execute(f"SELECT count(*) FROM {tf.table} WHERE volume = 0").fetchone()[0]
    assert got_zero == exp_zero
    if tf.is_intraday:
        assert got_zero > 0
        assert db.con.execute("SELECT sum(zero_volume_rows) FROM download_windows").fetchone()[0] == got_zero


def test_missing_bars_are_not_forward_filled(env):
    tf, db, fake = env
    run(db, fake, tf)
    sim = fake.instruments[900003]                                # illiquid: ~30% of bars have no trades
    got = rows(db, 900003)
    exp = expected(sim, tf)
    assert len(got) == len(exp)
    if tf.is_intraday:
        sessions = len({e[0].date() for e in exp})
        assert len(got) < 25 * sessions                           # gaps stay gaps


# ----------------------------------------------------------- idempotency / increments
def test_rerun_is_a_noop_and_refetch_creates_no_duplicates(env):
    tf, db, fake = env
    run(db, fake, tf)
    before, state = rows(db), window_states(db)
    n_requests = len(fake.log)
    stats, _ = run(db, fake, tf)
    assert len(fake.log) == n_requests and stats["attempted"] == 0       # nothing re-requested
    assert rows(db) == before and window_states(db) == state
    stats, _ = run(db, fake, tf, refetch=True)                           # forced re-download
    assert stats["attempted"] > 0 and stats["rows_inserted"] == 0 and stats["rows_revised"] == 0
    assert rows(db) == before
    assert db.con.execute("SELECT count(*) FROM candle_revisions").fetchone()[0] == 0


def test_incremental_update_only_requests_the_new_range_plus_overlap(env):
    tf, db, fake = env
    run(db, fake, tf)
    base = rows(db)
    later = CUTOFF + timedelta(days=12)
    stats, _ = run(db, fake, tf, cutoff=later)
    new_reqs = fake.log[-stats["attempted"]:]
    assert stats["attempted"] == 3 and len({r["payload"]["securityId"] for r in new_reqs}) == 3
    for r in new_reqs:
        p = r["payload"]
        assert p["fromDate"].startswith((CUTOFF - timedelta(days=7)).strftime("%Y-%m-%d"))
        assert p["toDate"].startswith(later.strftime("%Y-%m-%d"))
    assert set(base) <= set(rows(db))                                    # old rows untouched
    # equals a fresh full download to the later cutoff
    with CandleDB(tf, db.path.parent / f"fresh_{tf.db_name}") as fresh:
        load_universe(fresh)
        run(fresh, fake, tf, cutoff=later)
        assert rows(fresh) == rows(db)
    assert db.con.execute("SELECT count(*) FROM candle_revisions").fetchone()[0] == 0


def test_backfill_extends_history_without_touching_newer_rows(env):
    tf, db, fake = env
    newer = datetime(2024, 3, 1)
    run(db, fake, tf, history_start=newer)
    base = rows(db)
    n = len(fake.log)
    run(db, fake, tf)                                                    # now backfill to the full start
    older_reqs = fake.log[n:]
    assert older_reqs and all(datetime.strptime(r["payload"]["toDate"][:10], "%Y-%m-%d") <= newer
                              for r in older_reqs)
    assert set(base) <= set(rows(db))


# ---------------------------------------------------------------- retry / resume
def test_transient_errors_are_retried_inside_a_window(env):
    tf, db, fake = env
    fake.fail(times=2, status=500, match=lambda p: p["securityId"] == "900001")
    stats, client = run(db, fake, tf)
    assert stats["retriable"] == 0 and stats["failed"] == 0
    assert db.con.execute("SELECT max(attempts) FROM download_windows").fetchone()[0] == 3


def test_exhausted_retries_mark_retriable_then_resume_completes_only_that_work(env):
    tf, db, fake = env
    fake.fail(times=10_000, status=503, match=lambda p: p["securityId"] == "900002")
    stats, _ = run(db, fake, tf)
    assert stats["status"] == "completed_with_errors" and stats["retriable"] >= 1
    assert window_states(db).get("retriable", 0) >= 1
    sec1 = rows(db, 900001)
    fake.rules.clear()
    n = len(fake.log)
    stats2, _ = run(db, fake, tf)
    resumed = fake.log[n:]
    assert {r["payload"]["securityId"] for r in resumed} == {"900002"}   # nothing else re-requested
    assert stats2["status"] == "completed" and "retriable" not in window_states(db)
    assert rows(db, 900001) == sec1
    assert len(rows(db, 900002)) == len(expected(fake.instruments[900002], tf))


def test_crash_between_fetch_and_commit_loses_nothing_and_duplicates_nothing(env, monkeypatch):
    tf, db, fake = env
    calls = {"n": 0}
    real = CandleDB.commit_window

    def crashing(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt("simulated kill -9 right after a fetch")
        return real(self, *a, **kw)

    monkeypatch.setattr(CandleDB, "commit_window", crashing)
    with pytest.raises(KeyboardInterrupt):
        run(db, fake, tf)
    assert db.con.execute("SELECT status FROM download_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0] == "aborted"
    committed = db.con.execute("SELECT count(*) FROM download_windows").fetchone()[0]
    assert committed == 2                                                # the 3rd window was never marked
    monkeypatch.setattr(CandleDB, "commit_window", real)
    stats, _ = run(db, fake, tf)
    assert stats["status"] == "completed"
    with CandleDB(tf, db.path.parent / f"clean_{tf.db_name}") as clean:
        load_universe(clean)
        run(clean, fake, tf)
        assert rows(clean) == rows(db)                                   # resumed result == uninterrupted result


def test_window_commit_is_atomic(env, monkeypatch):
    tf, db, fake = env
    inst = db.get_instruments(symbols=["SIMALPHA"])[0]

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(CandleDB, "_upsert_window", boom)
    with pytest.raises(RuntimeError):
        run(db, fake, tf, symbols=("SIMALPHA",))
    assert n_rows(db) == 0                                               # candles rolled back
    assert db.con.execute("SELECT count(*) FROM download_windows").fetchone()[0] == 0
    assert db.covered_intervals(inst) == []


# ------------------------------------------------------------- failure classification
def test_permanent_failure_is_recorded_and_not_retried_unless_asked(env):
    tf, db, fake = env
    fake.fail(times=1, status=400, match=lambda p: p["securityId"] == "900001",
              body={"errorType": "Input_Exception", "errorCode": "DH-905", "errorMessage": "bad window"})
    stats, _ = run(db, fake, tf)
    assert stats["failed"] == 1 and stats["status"] == "completed_with_errors"
    row = db.con.execute("SELECT status, error_code, http_status FROM download_windows WHERE status = 'failed'").fetchone()
    assert row == ("failed", "DH-905", 400)
    n = len(fake.log)
    stats2, _ = run(db, fake, tf)
    assert len(fake.log) == n                                            # failed windows are not auto-retried
    stats3, _ = run(db, fake, tf, retry_failed=True)
    assert stats3["attempted"] == 1 and stats3["complete"] + stats3["empty"] == 1
    assert "failed" not in window_states(db)


def test_response_that_fails_validation_inserts_nothing(env):
    tf, db, fake = env
    if tf.is_intraday:
        fake.epoch_offset = 315532800                                    # pre-v2 1980-based epoch
    else:
        fake.inclusive_to = True                                         # API whose toDate is inclusive
    stats, _ = run(db, fake, tf, symbols=("SIMALPHA",))
    assert stats["failed"] >= 1
    codes = {r[0] for r in db.con.execute("SELECT error_code FROM download_windows WHERE status='failed'").fetchall()}
    assert codes == {"out_of_window"}
    failed = db.con.execute("SELECT count(*) FROM download_windows WHERE status = 'failed'").fetchone()[0]
    assert failed == stats["failed"]
    leaked = db.con.execute(f"SELECT count(*) FROM {tf.table} c JOIN download_windows w USING (window_id) "
                            "WHERE w.status = 'failed'").fetchone()[0]
    assert leaked == 0                                                   # a failed window contributes no rows


def test_empty_windows_are_tracked_separately_from_complete_and_failed(env):
    tf, db, fake = env
    stats, _ = run(db, fake, tf)
    states = window_states(db)
    assert states.get("empty", 0) >= 1 and states.get("complete", 0) >= 1 and "failed" not in states
    empties = db.con.execute("SELECT DISTINCT security_id FROM download_windows WHERE status = 'empty'").fetchall()
    assert empties == [(900002,)]                                        # only the pre-listing windows


def test_error_code_for_no_data_is_not_assumed_empty_unless_opted_in(env):
    tf, db, fake = env
    fake.empty_error_code = "DH-907"
    stats, _ = run(db, fake, tf, symbols=("SIMBETA",))
    assert stats["failed"] >= 1 and "empty" not in window_states(db)     # fail-closed by default
    stats2, _ = run(db, fake, tf, symbols=("SIMBETA",), retry_failed=True, empty_error_codes=frozenset({"DH-907"}))
    assert "failed" not in window_states(db)
    code = db.con.execute("SELECT error_code FROM download_windows WHERE status = 'empty'").fetchone()[0]
    assert code == "DH-907"                                              # the reason is kept on the row


def test_bad_token_aborts_run_and_marks_nothing_done(env):
    tf, db, fake = env
    stats, _ = run(db, fake, tf, client=make_client(fake, token="expired"))
    assert stats["status"] == "aborted" and "token" in stats["note"]
    assert db.con.execute("SELECT count(*) FROM download_windows").fetchone()[0] == 0
    assert db.con.execute("SELECT status FROM download_runs").fetchone()[0] == "aborted"


def test_circuit_breaker_stops_a_run_when_the_api_is_down(env):
    tf, db, fake = env
    fake.fail(times=10_000, status=500)
    stats, _ = run(db, fake, tf, breaker=3, client=make_client(fake, max_attempts=2))
    assert stats["status"] == "aborted" and "consecutive" in stats["note"]
    assert window_states(db) == {"retriable": 3}


def test_retroactive_revision_in_the_overlap_is_detected_and_not_overwritten(env):
    tf, db, fake = env
    run(db, fake, tf, symbols=("SIMALPHA",))
    before = rows(db)
    # the source now serves history adjusted for an event dated after the overlap
    sim = fake.instruments[900001]
    sim.split_on, sim.split_factor = date(2024, 6, 10), 0.5
    stats, _ = run(db, fake, tf, symbols=("SIMALPHA",), cutoff=CUTOFF + timedelta(days=12))
    assert stats["rows_revised"] > 0
    revs = db.con.execute("SELECT count(*) FROM candle_revisions").fetchone()[0]
    assert revs == stats["rows_revised"]
    assert set(before) <= set(rows(db))                                  # first-seen raw rows preserved


def test_window_larger_than_cap_is_refused(env):
    tf, db, fake = env
    if tf.is_intraday:
        with pytest.raises(ValueError):
            run(db, fake, tf, window_days=91)


def test_selection_is_explicit_no_implicit_full_universe(env):
    tf, db, fake = env
    with pytest.raises(SystemExit):
        select_instruments(db, symbols=None, security_ids=None, select_all=False)
    with pytest.raises(SystemExit):
        select_instruments(db, symbols=["NOPE"], security_ids=None, select_all=False)
    assert len(select_instruments(db, symbols=None, security_ids=None, select_all=True)) == 3
    assert len(select_instruments(db, symbols=None, security_ids=None, select_all=True, limit=2)) == 2
