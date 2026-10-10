"""Coverage and reconciliation reports: sessions inferred from the data, missing sessions, stale instruments, abnormal volume,
provider disagreements and instrument lifecycle. Everything is read-only and nothing is ever filled in."""
from datetime import date, datetime, time, timedelta

import pandas as pd

from fake_dhan import IST
from stockdata import reconcile
from stockdata.db import CandleDB
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.validation import validate_response
from stockdata.windows import Window
from conftest import load_universe

WIN = Window(datetime(2024, 5, 1), datetime(2024, 7, 30))
HOLIDAY = date(2024, 5, 20)


def epoch(dt):
    return int(dt.replace(tzinfo=IST).timestamp())


def store(db, symbol, rows, provider=None):
    inst = db.get_instruments(symbols=[symbol])[0]
    body = {"open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows], "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows], "timestamp": [epoch(r[0]) for r in rows]}
    v = validate_response(db.tf, body, WIN)
    db.commit_window(inst, WIN, candles=v.candles, anomalies=v.anomalies, zero_volume=v.zero_volume_rows, attempts=1, http_status=200,
                     provider=provider)


def sessions_of(first, last, *, skip=()):
    d, out = first, []
    while d <= last:
        if d.weekday() < 5 and d not in skip:
            out.append(datetime.combine(d, time()))
        d += timedelta(days=1)
    return out


def daily_rows(days, volume=100, close=100.0):
    return [(d, close, close + 1, close - 1, close, volume) for d in days]


def test_a_holiday_is_recognised_from_the_data_and_only_a_real_hole_is_reported_missing(tmp_path):
    days = sessions_of(date(2024, 5, 6), date(2024, 5, 31), skip=(HOLIDAY,))            # nobody traded on the holiday
    hole = datetime(2024, 5, 15)
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        load_universe(db)
        store(db, "SIMALPHA", daily_rows(days))
        store(db, "SIMBETA", daily_rows([d for d in days if d != hole]))
        store(db, "SIMGAMMA", daily_rows(days))
        r = reconcile.missing_sessions(db)
        assert r["sessions_known"] == len(days) and HOLIDAY.isoformat() not in [str(s) for s in reconcile.sessions(db)]
        assert r["instruments_with_data"] == 3 and r["instruments_with_missing"] == 1 and r["missing_total"] == 1
        row = r["rows"][0]
        assert row["symbol"] == "SIMBETA" and row["missing_sessions"] == 1 and row["sample"] == ["2024-05-15"]
        assert row["expected_sessions"] == len(days) and "NEVER filled in" in r["note"]
        assert db.con.execute("SELECT count(*) FROM candles_daily").fetchone()[0] == 3 * len(days) - 1      # nothing was added by the report


def test_an_instrument_is_only_expected_inside_its_own_stored_range(tmp_path):
    days = sessions_of(date(2024, 5, 6), date(2024, 5, 31))
    late = [d for d in days if d >= datetime(2024, 5, 20)]                               # a recent listing: no candles before 20 May
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        load_universe(db)
        store(db, "SIMALPHA", daily_rows(days))
        store(db, "SIMBETA", daily_rows(late))
        assert reconcile.missing_sessions(db)["missing_total"] == 0                      # not "missing" before it listed


def test_stale_instruments_are_those_behind_the_latest_session(tmp_path):
    days = sessions_of(date(2024, 5, 6), date(2024, 5, 31))
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        load_universe(db)
        store(db, "SIMALPHA", daily_rows(days))
        store(db, "SIMBETA", daily_rows(days))
        store(db, "SIMGAMMA", daily_rows([d for d in days if d <= datetime(2024, 5, 24)]))
        r = reconcile.stale(db)
        assert r["latest_session"] == "2024-05-31" and r["instruments_behind"] == 1 and r["instruments_current"] == 2
        assert r["rows"] == [{"symbol": "SIMGAMMA", "security_id": 900003, "last_candle_day": "2024-05-24", "sessions_behind": 5}]


def test_abnormal_volume_is_a_lead_against_the_previous_20_session_median(tmp_path):
    days = sessions_of(date(2024, 5, 6), date(2024, 6, 14))
    rows = daily_rows(days)
    rows[-1] = (rows[-1][0], 100, 101, 99, 100, 5000)                                    # 50x the median of the previous sessions
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        load_universe(db)
        store(db, "SIMALPHA", rows)
        r = reconcile.abnormal_volume(db, since_days=20000)
        assert r["total"] == 1 and r["rows"][0]["date"] == "2024-06-14" and r["rows"][0]["ratio"] == 50.0
        assert "not defects" in r["note"]


def test_provider_disagreements_keep_the_stored_value_and_are_listed_by_pair(tmp_path):
    days = sessions_of(date(2024, 5, 6), date(2024, 5, 10))
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        load_universe(db)
        store(db, "SIMALPHA", daily_rows(days, close=100.0), provider="dhan")
        store(db, "SIMALPHA", daily_rows(days[:2], close=101.0), provider="fyers")           # same candles, different values
        r = reconcile.provider_disagreements(db)
        assert r["total"] == 2 and r["pairs"][0]["stored_provider"] == "dhan" and r["pairs"][0]["other_provider"] == "fyers"
        assert r["pairs"][0]["candles"] == 2 and r["by_instrument"] == [{"symbol": "SIMALPHA", "security_id": 900001, "candles": 2}]
        assert {s["stored_close"] for s in r["sample"]} == {100.0} and {s["other_close"] for s in r["sample"]} == {101.0}
        stored = db.con.execute("SELECT DISTINCT close, provider FROM candles_daily").fetchall()
        assert stored == [(100.0, "dhan")]                                                 # the first-seen candle was kept


def test_final_candle_completeness_flags_sessions_that_look_cut_short(tmp_path):
    full = datetime.combine(date(2024, 5, 6), time(9, 15))
    complete = [full + timedelta(minutes=15 * i) for i in range(26)]                    # 09:15 .. 15:30 starts: ends with the 15:15 bar
    complete = [b for b in complete if b.time() <= time(15, 15)]
    short = [datetime.combine(date(2024, 5, 7), time(9, 15)) + timedelta(minutes=15 * i) for i in range(10)]    # stops at 11:30
    f_and_o = [datetime.combine(date(2024, 5, 8), time(9, 15)) + timedelta(minutes=15 * i) for i in range(24)]  # last bar starts 15:00
    with CandleDB(INTRADAY_15, tmp_path / "i.duckdb") as db:
        load_universe(db)
        store(db, "SIMALPHA", daily_rows(complete + short + f_and_o))
        r = reconcile.final_candle_completeness(db)
        assert r["sessions_checked"] == 3 and r["sessions_ending_early"] == 1
        assert r["rows"][0]["symbol"] == "SIMALPHA" and r["rows"][0]["latest"] == "2024-05-07" and r["rows"][0]["sessions_with_few_bars"] == 1
        assert "15:00 for F&O" in r["note"]
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        assert reconcile.final_candle_completeness(db) is None                           # a daily database has no intraday sessions


def test_build_returns_every_section_on_a_database_with_no_candles(tmp_path):
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        load_universe(db)
        out = reconcile.build(db)
        assert set(out) == {"missing_sessions", "stale", "abnormal_volume", "provider_disagreements"}
        assert out["missing_sessions"]["rows"] == [] and out["stale"]["latest_session"] is None
    with CandleDB(INTRADAY_15, tmp_path / "i.duckdb") as db:
        assert "final_candle" in reconcile.build(db)


def test_lifecycle_keeps_identity_by_security_id_never_by_symbol(tmp_path):
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        load_universe(db)
        h = lambda sid, sym, isin, frm, to: db.con.execute(
            "INSERT INTO instrument_history (history_id, exchange_segment, security_id, symbol, name, isin, series, valid_from, valid_to) "
            "VALUES (?, 'NSE_EQ', ?, ?, ?, ?, 'EQ', ?, ?)", [db.next_id("instrument_history", "history_id"), sid, sym, sym, isin, frm, to])
        h(910001, "OLDNAME", "INE000A01011", date(2023, 1, 1), date(2024, 1, 1))       # a rename: same security id, new symbol
        h(910001, "NEWNAME", "INE000A01011", date(2024, 1, 2), None)
        h(910002, "REUSED", "INE000B01012", date(2022, 1, 1), date(2023, 6, 1))        # a symbol reused by a DIFFERENT security
        h(910003, "REUSED", "INE000C01013", date(2023, 6, 2), None)
        h(910004, "SHARE1", "INE000D01014", date(2023, 1, 1), None)                    # one ISIN on two security ids
        h(910005, "SHARE2", "INE000D01014", date(2023, 1, 1), None)
        db.con.execute("UPDATE instruments SET is_active = false, last_seen = DATE '2024-04-01' WHERE security_id = 900003")
        r = reconcile.lifecycle(db)
        assert [x["symbols"] for x in r["renamed"]] == [["OLDNAME", "NEWNAME"]] and r["renamed"][0]["security_id"] == 910001
        assert r["symbol_reused"] == [{"symbol": "REUSED", "security_ids": sorted(r["symbol_reused"][0]["security_ids"])}]
        assert sorted(r["symbol_reused"][0]["security_ids"]) == [910002, 910003]
        assert sorted(r["isin_shared"][0]["security_ids"]) == [910004, 910005] and r["isin_shared"][0]["isin"] == "INE000D01014"
        assert [x["symbol"] for x in r["left_the_master"]] == ["SIMGAMMA"]
        assert any("NOT linked automatically" in n for n in r["notes"])
