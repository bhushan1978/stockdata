"""Explicit historical refresh, on scratch databases with unchanged AND deliberately revised source data.

The synthetic server stands in for Dhan: setting `split_on`/`split_factor` makes it serve retroactively revised history,
`listing` moves the first available candle, and `fail(...)` injects errors and empty answers.
"""
from datetime import date, datetime, timedelta

import pytest

from conftest import CUTOFF, DAILY_START, INTRADAY_START, load_universe, make_client, run, sha256_file
from fake_dhan import bars_for_day, daily_for_day
from stockdata import corp_actions as ca
from stockdata.budget import RequestBudget, UsageLedger
from stockdata.db import CandleDB
from stockdata.downloader import RunAborted
from stockdata.refresh import Refresher, RefreshOptions
from stockdata.reports import integrity_checks
from stockdata.timeframes import DAILY, INTRADAY_15

WINDOW = {"daily": 365, "15min": 89}
BOTH = ("SIMALPHA", "SIMBETA")


@pytest.fixture(params=["daily", "15min"])
def renv(request, tmp_path, fake):
    tf = DAILY if request.param == "daily" else INTRADAY_15
    with CandleDB(tf, tmp_path / tf.db_name) as db:
        load_universe(db)
        run(db, fake, tf, symbols=BOTH)                       # the initial (first-seen) download
        yield tf, db, fake


def start_of(tf):
    return INTRADAY_START if tf.is_intraday else DAILY_START


def opts_for(tf, **kw):
    return RefreshOptions(start=kw.pop("start", start_of(tf)), end=kw.pop("end", CUTOFF), window_days=WINDOW[tf.name], **kw)


def refresh(db, fake, symbols=BOTH, client=None, **kw):
    insts = db.get_instruments(symbols=list(symbols))
    return Refresher(db, client or make_client(fake)).run(insts, opts_for(db.tf, **kw))


def snap(db):
    return db.con.execute(f"SELECT * FROM {db.tf.table} ORDER BY exchange_segment, security_id, {db.tf.key_col}").fetchall()


def values(db, sec):
    t = db.tf
    return {r[0]: r[1:] for r in db.con.execute(
        f"SELECT {t.key_col}, open, high, low, close, volume FROM {t.table} WHERE security_id = ? ORDER BY 1", [sec]).fetchall()}


def truth(sim, tf, start=None, end=CUTOFF):
    """What the synthetic source serves right now for [start, end)."""
    out, d = {}, (start or start_of(tf)).date()
    while d < end.date():
        if tf.is_intraday:
            for b in bars_for_day(sim, d):
                out[b["ts"].replace(tzinfo=None)] = (b["open"], b["high"], b["low"], b["close"], b["volume"])
        else:
            c = daily_for_day(sim, d)
            if c:
                out[d] = (c["open"], c["high"], c["low"], c["close"], c["volume"])
        d += timedelta(days=1)
    return out


def count(db, table):
    return db.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def revise(fake):
    sim = fake.instruments[900001]
    sim.split_on, sim.split_factor = date(2024, 3, 10), 0.5      # the source now serves revised history before this date
    return sim


# ------------------------------------------------------------------ unchanged data
def test_unchanged_source_data_changes_nothing_and_a_repeat_is_a_noop(renv):
    tf, db, fake = renv
    before = snap(db)                                              # includes ingested_at_utc and window ids
    n0 = len(fake.log)
    stats = refresh(db, fake)
    assert stats["status"] == "completed" and stats["requests_made"] == len(fake.log) - n0 == 4
    assert snap(db) == before                                      # not a single stored value or timestamp touched
    assert count(db, "candle_revisions") == count(db, "refresh_changes") == 0
    statuses = {r[0] for r in db.con.execute("SELECT status FROM refresh_windows").fetchall()}
    assert statuses <= {"unchanged", "empty"}
    assert dict(db.con.execute("SELECT status, count(*) FROM refresh_jobs GROUP BY 1").fetchall()) == {"complete": 2}
    stats2 = refresh(db, fake)                                     # repeating a completed refresh: a new job, still a no-op
    assert stats2["status"] == "completed" and snap(db) == before
    assert dict(db.con.execute("SELECT status, count(*) FROM refresh_jobs GROUP BY 1").fetchall()) == {"complete": 4}


# ---------------------------------------------------------------- revised data
def test_revised_history_replaces_stored_candles_and_records_old_versus_new(renv):
    tf, db, fake = renv
    before = {r[:3]: r for r in snap(db)}
    old_alpha, old_beta = values(db, 900001), values(db, 900002)
    sim = revise(fake)
    stats = refresh(db, fake, ["SIMALPHA"])
    assert stats["status"] == "completed" and stats["replaced"] >= 1
    expected = truth(sim, tf)
    assert values(db, 900001) == expected and expected != old_alpha         # stored == the source's current data
    assert len(snap(db)) == len(before)                                      # nothing added or lost
    after = {r[:3]: r for r in snap(db)}
    changed = {k for k in before if before[k][3:8] != after[k][3:8]}
    assert changed and all(k[1] == 900001 for k in changed)
    assert all(after[k] == before[k] for k in before if k not in changed)    # every other candle is byte-identical
    assert values(db, 900002) == old_beta                                    # the other instrument was not selected: untouched
    # the audit trail: old and new for exactly the changed candles
    revs = db.con.execute("SELECT candle_key, old_open, old_high, old_low, old_close, old_volume, new_open, new_high, new_low, "
                          "new_close, new_volume FROM candle_revisions").fetchall()
    assert len(revs) == len(changed)
    for r in revs:
        key = r[0].date() if not tf.is_intraday else r[0]
        assert tuple(r[1:6]) == old_alpha[key] and tuple(r[6:11]) == expected[key] and old_alpha[key] != expected[key]
    ch = db.con.execute("SELECT change_type, count(*), count(DISTINCT job_id) FROM refresh_changes GROUP BY 1").fetchall()
    assert ch == [("revised", len(changed), 1)]
    # changed candles carry a new ingest time; the job and its windows are recorded
    assert db.con.execute(f"SELECT count(*) FROM {tf.table} WHERE security_id = 900001 AND ingested_at_utc > "
                          "(SELECT min(applied_at_utc) - INTERVAL 1 HOUR FROM refresh_windows)").fetchone()[0] >= len(changed)
    assert stats["results"][0].changed == len(changed)
    # idempotent: repeating the very same refresh finds nothing left to change
    again = refresh(db, fake, ["SIMALPHA"])
    assert again["results"][0].changed == 0 and values(db, 900001) == expected
    assert count(db, "refresh_changes") == len(changed) and count(db, "candle_revisions") == len(changed)
    assert all(c.ok for c in integrity_checks(db))


def test_revised_data_is_stored_as_received_with_no_adjustment_applied(tmp_path, fake):
    """A verified corporate action exists in the daily database; the refresh must still store Dhan's data untouched."""
    tf = DAILY
    with CandleDB(tf, tmp_path / "d.duckdb") as db:
        load_universe(db)
        run(db, fake, tf, symbols=("SIMALPHA",))
        ca.import_actions(db.con, [{"symbol": "SIMALPHA", "event_type": "BONUS", "ex_date": "2024-02-01", "ratio_text": "1:1",
                                    "bonus_new": 1, "bonus_held": 1}], source="NSE", source_ref="t", source_confirmed=True)
        aid = db.con.execute("SELECT action_id FROM corporate_actions").fetchone()[0]
        ca.set_manual_status(db.con, aid, "verified", "test fixture")
        ca.apply_adjustments(db.con, tf)
        segs = db.con.execute("SELECT * FROM adjustment_segments ORDER BY ALL").fetchall()
        actions = db.con.execute("SELECT * FROM corporate_actions ORDER BY ALL").fetchall()
        sim = revise(fake)
        refresh(db, fake, ["SIMALPHA"])
        raw = values(db, 900001)
        assert raw == truth(sim, tf)                                           # raw == exactly what the source sent
        assert db.con.execute("SELECT * FROM adjustment_segments ORDER BY ALL").fetchall() == segs
        assert db.con.execute("SELECT * FROM corporate_actions ORDER BY ALL").fetchall() == actions
        d0 = date(2024, 1, 10)                                                 # before the ex-date: the VIEW applies the factor, the table does not
        adj = db.con.execute("SELECT close FROM candles_daily_adjusted WHERE security_id = 900001 AND candle_date = ?", [d0]).fetchone()[0]
        assert adj == pytest.approx(raw[d0][3] * 0.5)


# ---------------------------------------------------- added / absent-from-source candles
def test_candles_missing_locally_are_added_and_the_database_stays_consistent(renv):
    tf, db, fake = renv
    k = tf.key_col
    gone = db.con.execute(f"SELECT {k} FROM {tf.table} WHERE security_id = 900001 ORDER BY {k} LIMIT 12 OFFSET 40").fetchall()
    db.con.execute(f"DELETE FROM {tf.table} WHERE security_id = 900001 AND {k} IN (SELECT {k} FROM {tf.table} WHERE security_id = 900001 "
                   f"ORDER BY {k} LIMIT 12 OFFSET 40)")
    n_before = count(db, tf.table)
    stats = refresh(db, fake, ["SIMALPHA"])
    assert stats["results"][0].added == 12 == count(db, tf.table) - n_before
    assert values(db, 900001) == truth(fake.instruments[900001], tf)
    assert db.con.execute("SELECT change_type, count(*) FROM refresh_changes GROUP BY 1").fetchall() == [("added", 12)]
    assert all(c.ok for c in integrity_checks(db))                          # every new candle belongs to a complete window


def test_fill_only_adds_missing_candles_but_never_replaces_a_stored_one(renv):
    tf, db, fake = renv
    k = tf.key_col
    sim = revise(fake)                                                      # the source now disagrees with what is stored (before 10 Mar)
    gone = [r[0] for r in db.con.execute(f"SELECT {k} FROM {tf.table} WHERE security_id = 900001 ORDER BY {k} LIMIT 12 OFFSET 40").fetchall()]
    db.con.execute(f"DELETE FROM {tf.table} WHERE security_id = 900001 AND {k} IN (SELECT {k} FROM {tf.table} WHERE security_id = 900001 "
                   f"ORDER BY {k} LIMIT 12 OFFSET 40)")
    kept = values(db, 900001)
    n_before = count(db, tf.table)
    stats = refresh(db, fake, ["SIMALPHA"], fill_only=True)
    expected = truth(sim, tf)
    assert stats["status"] == "completed" and stats["results"][0].added == 12 == count(db, tf.table) - n_before
    after = values(db, 900001)
    assert {key: after[key] for key in kept} == kept                        # every candle that was stored is byte-identical afterwards
    assert all(after[g] == expected[g] for g in gone)                       # the holes now hold what the source serves
    assert any(after[key] != expected[key] for key in after)                # ... while the stored history it disagrees with was left alone
    assert db.con.execute("SELECT change_type, count(*) FROM refresh_changes GROUP BY 1").fetchall() == [("added", 12)]   # no 'revised' row ever
    assert count(db, "candle_revisions") > 0                                # the disagreement is recorded for review, not applied
    assert db.con.execute("SELECT fill_only FROM refresh_jobs").fetchall() == [(True,)]
    assert all(c.ok for c in integrity_checks(db))


def test_a_fill_only_job_and_a_replacing_job_are_separate_jobs_and_replace_still_replaces(renv):
    tf, db, fake = renv
    sim = revise(fake)
    refresh(db, fake, ["SIMALPHA"], fill_only=True)                         # nothing missing: stores nothing
    assert values(db, 900001) != truth(sim, tf)
    refresh(db, fake, ["SIMALPHA"])                                         # the explicit repair: replaces
    assert values(db, 900001) == truth(sim, tf)
    assert db.con.execute("SELECT coalesce(fill_only, false), count(*) FROM refresh_jobs GROUP BY 1 ORDER BY 1").fetchall() == [(False, 1), (True, 1)]


def _absent_scenario(tf, fake):
    sim = fake.instruments[900001]
    sim.listing = date(2023, 9, 1) if not tf.is_intraday else date(2024, 2, 1)   # earlier candles are no longer served
    return sim


def test_candles_absent_from_the_source_are_kept_unless_pruning_is_requested(renv):
    tf, db, fake = renv
    sim = _absent_scenario(tf, fake)
    before = snap(db)
    stats = refresh(db, fake, ["SIMALPHA"])
    r = stats["results"][0]
    assert r.missing > 0 and r.removed == 0 and snap(db) == before          # reported, never deleted by default
    assert stats["status"] == "completed_with_issues"
    kept = db.con.execute("SELECT count(*) FROM refresh_changes WHERE change_type = 'missing_kept'").fetchone()[0]
    assert kept == r.missing
    # pruning is explicit, archived first, and limited to windows with a validated NON-EMPTY response
    stats2 = refresh(db, fake, ["SIMALPHA"], prune_missing=True)
    r2 = stats2["results"][0]
    removed_rows = db.con.execute("SELECT count(*), count(old_close), count(new_close) FROM refresh_changes "
                                  "WHERE change_type = 'removed'").fetchone()
    assert removed_rows == (r2.removed, r2.removed, 0)                       # archived with their old values
    assert len(snap(db)) == len(before) - r2.removed
    present = values(db, 900001)
    assert all(present[key] == v for key, v in truth(sim, tf).items())      # what the source serves is stored
    if tf.is_intraday:
        assert r2.removed == r.missing                                       # every absent candle sat in a non-empty window
    else:
        # the oldest daily window got an EMPTY answer: ambiguous, so its stored candles were held, not deleted
        held = db.con.execute("SELECT count(*) FROM refresh_windows WHERE status = 'held_empty'").fetchone()[0]
        assert held >= 1 and r2.removed < r.missing + 1


# ---------------------------------------------- failures and ambiguity never change a candle
FAILURES = ["auth", "server_error", "rate_limit", "bad_request", "garbage", "validation", "empty_response", "dh907"]


@pytest.mark.parametrize("mode", FAILURES)
def test_failures_and_ambiguous_responses_never_change_a_stored_candle(renv, mode):
    tf, db, fake = renv
    sim = revise(fake)                                                      # there IS something to change, so a bug would show
    before = snap(db)
    client = make_client(fake, max_attempts=2)
    if mode == "auth":
        client = make_client(fake, token="expired", max_attempts=2)
    elif mode == "server_error":
        fake.fail(times=10_000, status=503)
    elif mode == "rate_limit":
        fake.fail(times=10_000, status=429, headers={"Retry-After": "0"},
                  body={"errorType": "Rate_Limit", "errorCode": "DH-904", "errorMessage": "too many"})
    elif mode == "bad_request":
        fake.fail(times=10_000, status=400, body={"errorType": "Input_Exception", "errorCode": "DH-905", "errorMessage": "bad"})
    elif mode == "garbage":
        fake.fail(times=10_000, status="garbage")
    elif mode == "validation":
        fake.epoch_offset = 315532800
    elif mode == "empty_response":
        fake.fail(times=10_000, status="empty")
    elif mode == "dh907":
        sim.listing = date(2031, 1, 1)
        fake.empty_error_code = "DH-907"
    stats = refresh(db, fake, ["SIMALPHA"], client=client)
    assert snap(db) == before                                               # the stored candles are exactly as they were
    assert count(db, "candle_revisions") == count(db, "refresh_changes") == 0
    window_states = {r[0] for r in db.con.execute("SELECT status FROM refresh_windows").fetchall()}
    job_states = dict(db.con.execute("SELECT status, count(*) FROM refresh_jobs GROUP BY 1").fetchall())
    expect = {"auth": ("aborted", {"pending"}), "server_error": ("completed_with_issues", {"retriable", "pending"}),
              "rate_limit": ("completed_with_issues", {"retriable", "pending"}),
              "bad_request": ("completed_with_issues", {"failed"}), "garbage": ("completed_with_issues", {"retriable", "pending"}),
              "validation": ("completed_with_issues", {"failed"}), "empty_response": ("completed_with_issues", {"held_empty"}),
              "dh907": ("completed_with_issues", {"failed"})}[mode]
    assert stats["status"] == expect[0] and window_states <= expect[1]
    if mode == "empty_response":
        assert job_states == {"complete": 1}                                # resolved (needs review), nothing deleted
    else:
        assert job_states == {"open": 1}                                    # unresolved: still resumable


def test_a_failure_while_applying_rolls_everything_back(renv, monkeypatch):
    tf, db, fake = renv
    revise(fake)
    before = snap(db)

    def boom(self, *a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(Refresher, "_set", boom)
    with pytest.raises(RuntimeError):
        refresh(db, fake, ["SIMALPHA"])
    assert snap(db) == before                                               # candle updates rolled back
    assert count(db, "candle_revisions") == count(db, "refresh_changes") == 0
    assert {r[0] for r in db.con.execute("SELECT status FROM refresh_windows").fetchall()} == {"pending"}


# --------------------------------------------------------------- resumable
def test_an_interrupted_refresh_resumes_the_same_job_without_repeating_applied_windows(renv, monkeypatch):
    tf, db, fake = renv
    sim = revise(fake)
    calls, real = {"n": 0}, Refresher._refresh_window

    def interrupt_second(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("simulated kill")
        return real(self, *a, **kw)

    monkeypatch.setattr(Refresher, "_refresh_window", interrupt_second)
    n0 = len(fake.log)
    with pytest.raises(KeyboardInterrupt):
        refresh(db, fake, ["SIMALPHA"])
    assert len(fake.log) - n0 == 1                                           # only the first window was fetched
    job = db.con.execute("SELECT job_id, status FROM refresh_jobs").fetchone()
    assert job[1] == "open"
    assert sorted(r[0] for r in db.con.execute("SELECT status FROM refresh_windows").fetchall()) == ["pending", "replaced"]
    assert db.con.execute("SELECT status FROM download_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0] == "aborted"
    monkeypatch.setattr(Refresher, "_refresh_window", real)
    n1 = len(fake.log)
    stats = refresh(db, fake, ["SIMALPHA"])                                  # the SAME command again
    assert len(fake.log) - n1 == 1                                           # only the pending window is requested
    assert stats["results"][0].resumed and stats["results"][0].job_id == job[0]
    assert values(db, 900001) == truth(sim, tf)
    assert count(db, "refresh_jobs") == 1 and db.con.execute("SELECT status FROM refresh_jobs").fetchone()[0] == "complete"
    assert count(db, "candle_revisions") == db.con.execute("SELECT count(*) FROM refresh_changes").fetchone()[0]   # no duplicates
    assert all(c.ok for c in integrity_checks(db))


def test_temporary_failures_are_retried_on_resume_and_only_unfinished_windows_are_requested(renv):
    tf, db, fake = renv
    sim = revise(fake)
    fake.fail(times=10_000, status=503)
    stats = refresh(db, fake, ["SIMALPHA"], client=make_client(fake, max_attempts=2))
    assert stats["results"][0].windows["retriable"] == 1 and values(db, 900001) != truth(sim, tf)
    fake.rules.clear()
    n = len(fake.log)
    stats2 = refresh(db, fake, ["SIMALPHA"])
    assert stats2["status"] == "completed" and stats2["results"][0].resumed
    assert len(fake.log) - n == 2                                            # both windows were still unfinished
    assert values(db, 900001) == truth(sim, tf)


def test_a_request_limit_stops_a_refresh_safely_and_it_resumes_later(renv, tmp_path):
    tf, db, fake = renv
    sim = revise(fake)
    ledger = UsageLedger(tmp_path / "ledger.sqlite")
    client = make_client(fake, budget=RequestBudget(ledger, tf.name, max_per_run=1, max_per_day=100))
    stats = refresh(db, fake, ["SIMALPHA"], client=client)
    assert stats["status"] == "stopped_request_limit" and stats["requests_made"] == 1
    assert sorted(r[0] for r in db.con.execute("SELECT status FROM refresh_windows").fetchall()) == ["pending", "replaced"]
    n = len(fake.log)
    refresh(db, fake, ["SIMALPHA"])
    assert len(fake.log) - n == 1 and values(db, 900001) == truth(sim, tf)


def test_failed_windows_are_skipped_until_retry_failed_is_requested(renv):
    tf, db, fake = renv
    sim = revise(fake)
    fake.fail(times=1, status=400, body={"errorType": "Input_Exception", "errorCode": "DH-905", "errorMessage": "bad"})
    first = refresh(db, fake, ["SIMALPHA"])                                  # newest window is rejected, the older one is applied
    assert first["status"] == "completed_with_issues"
    assert sorted(r[0] for r in db.con.execute("SELECT status FROM refresh_windows").fetchall()) == ["failed", "replaced"]
    n = len(fake.log)
    again = refresh(db, fake, ["SIMALPHA"])                                  # resumes the open job: the failed window is NOT retried
    assert len(fake.log) == n and again["results"][0].resumed
    assert db.con.execute("SELECT status FROM refresh_jobs").fetchone()[0] == "open"
    refresh(db, fake, ["SIMALPHA"], retry_failed=True)                       # now it is
    assert len(fake.log) == n + 1
    assert {r[0] for r in db.con.execute("SELECT status FROM refresh_windows").fetchall()} == {"replaced"}
    assert db.con.execute("SELECT status FROM refresh_jobs").fetchone()[0] == "complete"
    assert values(db, 900001) == truth(sim, tf)


# ---------------------------------------------------------------- independence
def test_daily_and_15min_refreshes_are_independent(tmp_path, fake):
    paths = {}
    for tf in (DAILY, INTRADAY_15):                                          # both databases are first downloaded BEFORE the source is revised
        with CandleDB(tf, tmp_path / tf.db_name) as db:
            load_universe(db)
            run(db, fake, tf, symbols=("SIMALPHA",))
        paths[tf.name] = tmp_path / tf.db_name
    sim = revise(fake)
    hash_intraday = sha256_file(paths["15min"])
    with CandleDB(DAILY, paths["daily"]) as db:
        refresh(db, fake, ["SIMALPHA"])
        assert values(db, 900001) == truth(sim, DAILY)
    assert sha256_file(paths["15min"]) == hash_intraday                      # the 15-minute file was not touched at all
    with CandleDB(INTRADAY_15, paths["15min"]) as db15:
        assert count(db15, "refresh_jobs") == 0                              # and knows nothing about the daily job
        assert values(db15, 900001) != truth(sim, INTRADAY_15)               # still the old (pre-revision) data
        refresh(db15, fake, ["SIMALPHA"])
        assert values(db15, 900001) == truth(sim, INTRADAY_15)
    with CandleDB(DAILY, paths["daily"], read_only=True) as db:
        assert count(db, "refresh_jobs") == 1                                # the 15-minute refresh added nothing here


def test_refresh_range_must_be_valid(renv):
    tf, db, fake = renv
    with pytest.raises(ValueError):
        refresh(db, fake, start=CUTOFF, end=CUTOFF)
    if tf.is_intraday:
        with pytest.raises(ValueError):
            Refresher(db, make_client(fake)).run(db.get_instruments(symbols=["SIMALPHA"]),
                                                 RefreshOptions(start=INTRADAY_START, end=CUTOFF, window_days=91))
