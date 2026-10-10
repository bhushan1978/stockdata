"""Integrity checks must accept legitimate, flagged special sessions and still catch unflagged oddities."""
from datetime import date, datetime, time, timedelta

import pytest

from fake_dhan import IST
from stockdata import config
from stockdata.db import CandleDB
from stockdata.instruments import Instrument
from stockdata.reports import integrity_checks
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.validation import validate_response
from stockdata.windows import Window

INST = Instrument("NSE_EQ", 900001, "SIMALPHA", "EQUITY")
WIN = Window(datetime(2024, 5, 1), datetime(2024, 7, 30))


def epoch(dt):
    return int(dt.replace(tzinfo=IST).timestamp())


def body(rows):
    return {"open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows],
            "close": [r[4] for r in rows], "volume": [r[5] for r in rows], "timestamp": [epoch(r[0]) for r in rows]}


def store(db, rows):
    v = validate_response(db.tf, body(rows), WIN)
    db.commit_window(INST, WIN, candles=v.candles, anomalies=v.anomalies, zero_volume=v.zero_volume_rows,
                     attempts=1, http_status=200)


def check(db, fragment):
    return next(c for c in integrity_checks(db) if fragment in c.name)


def test_flagged_sunday_session_passes_but_an_unflagged_weekend_candle_fails(tmp_path):
    days = [datetime(2024, 5, 6) + timedelta(days=i) for i in range(60)]
    rows = [(d, 100, 101, 99, 100, 5) for d in days if d.weekday() < 5][:30]
    rows.append((datetime(2024, 5, 5), 100, 101, 99, 100, 5))           # a Sunday special session
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        store(db, rows)
        c = check(db, "weekday, or flagged")
        assert c.ok and "1 flagged weekend candle" in c.detail
        db.con.execute("INSERT INTO candles_daily (exchange_segment, security_id, candle_date, open, high, low, close, volume, epoch_raw, "
                       "window_id, ingested_at_utc) SELECT exchange_segment, security_id, DATE '2024-05-12', open, high, low, "
                       "close, volume, epoch_raw, window_id, ingested_at_utc FROM candles_daily LIMIT 1")   # a Sunday, never flagged
        assert not check(db, "weekday, or flagged").ok


def test_flagged_evening_bar_passes_but_an_unflagged_off_grid_bar_fails(tmp_path):
    day = datetime(2024, 5, 6)
    rows = [(datetime.combine(day, time(9, 15)) + timedelta(minutes=15 * i), 100, 101, 99, 100, 5) for i in range(25)]
    rows.append((datetime(2024, 5, 6, 18, 15), 100, 101, 99, 100, 5))   # muhurat-style evening bar
    with CandleDB(INTRADAY_15, tmp_path / "i.duckdb") as db:
        store(db, rows)
        c = check(db, "15-minute grid")
        assert c.ok and "1 flagged special-session bar" in c.detail
        db.con.execute("INSERT INTO candles_15min (exchange_segment, security_id, candle_timestamp, open, high, low, close, volume, "
                       "epoch_raw, window_id, ingested_at_utc) SELECT exchange_segment, security_id, TIMESTAMP '2024-05-06 10:07:00', "
                       "open, high, low, close, volume, epoch_raw, window_id, ingested_at_utc FROM candles_15min LIMIT 1")
        assert not check(db, "15-minute grid").ok


def test_daily_default_window_covers_the_whole_history_in_one_request():
    # Live finding: empty daily windows are an ambiguous DH-907 error, so avoid creating pre-listing windows.
    assert DAILY.default_window_days >= (date.today() - config.DAILY_HISTORY_START).days
    assert INTRADAY_15.default_window_days <= INTRADAY_15.max_window_days == 90
