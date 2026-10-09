from datetime import date, datetime, time, timedelta

import pytest

from fake_dhan import IST
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.validation import ValidationError, validate_response
from stockdata.windows import Window

WIN_I = Window(datetime(2024, 5, 1), datetime(2024, 5, 31))
WIN_D = Window(datetime(2024, 5, 1), datetime(2024, 5, 31))


def epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=IST).timestamp())


def body(rows):
    """rows: (datetime_ist, o, h, l, c, v)"""
    return {"open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows],
            "close": [r[4] for r in rows], "volume": [r[5] for r in rows],
            "timestamp": [epoch(r[0]) for r in rows], "open_interest": [0] * len(rows)}


def bars(day=date(2024, 5, 6), n=5):
    start = datetime.combine(day, time(9, 15))
    return [(start + timedelta(minutes=15 * i), 100.0, 101.0, 99.0, 100.5, 10 * i) for i in range(n)]


def test_intraday_converted_to_ist_and_zero_volume_kept():
    v = validate_response(INTRADAY_15, body(bars()), WIN_I)
    assert list(v.candles["candle_timestamp"].dt.strftime("%H:%M")) == ["09:15", "09:30", "09:45", "10:00", "10:15"]
    assert v.n == 5 and v.zero_volume_rows == 1          # volume 0 is data, not an error
    assert (v.candles["volume"] == [0, 10, 20, 30, 40]).all()
    assert v.candles["epoch_raw"].iloc[0] == epoch(datetime(2024, 5, 6, 9, 15))
    assert v.anomalies.empty


def test_daily_midnight_ist_epoch_maps_to_that_date():
    rows = [(datetime(2024, 5, 6), 1, 2, 1, 2, 5), (datetime(2024, 5, 7), 2, 3, 2, 3, 6)]
    v = validate_response(DAILY, body(rows), WIN_D)
    assert [d.date() for d in v.candles["candle_date"]] == [date(2024, 5, 6), date(2024, 5, 7)]


def test_empty_arrays_are_a_valid_empty_window():
    empty = {k: [] for k in ("open", "high", "low", "close", "volume", "timestamp")}
    v = validate_response(INTRADAY_15, empty, WIN_I)
    assert v.n == 0


@pytest.mark.parametrize("bad", [{}, None, [], {"open": []}, {"status": "failure", "remarks": "x"}])
def test_missing_arrays_are_malformed_never_empty(bad):
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, bad, WIN_I)
    assert e.value.code == "malformed_response"


def test_length_mismatch_rejected():
    b = body(bars())
    b["close"] = b["close"][:-1]
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, b, WIN_I)
    assert e.value.code == "length_mismatch"


@pytest.mark.parametrize("field,value,code", [
    ("open", None, "non_numeric_price"), ("high", float("nan"), "non_numeric_price"),
    ("volume", None, "non_numeric_volume"), ("volume", -5, "negative_volume"),
    ("volume", 12.5, "non_integer_volume"), ("timestamp", None, "non_numeric_timestamp")])
def test_bad_values_rejected(field, value, code):
    b = body(bars())
    b[field][2] = value
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, b, WIN_I)
    assert e.value.code == code


def test_integral_float_volume_accepted():
    b = body(bars())
    b["volume"] = [float(x) for x in b["volume"]]
    assert validate_response(INTRADAY_15, b, WIN_I).n == 5


def test_candle_outside_requested_window_fails_the_whole_window():
    # e.g. an API whose toDate is inclusive, contrary to the exclusive-boundary assumption
    w = Window(datetime(2024, 5, 1), datetime(2024, 5, 7))
    rows = [(datetime(2024, 5, 6), 1, 2, 1, 2, 5), (datetime(2024, 5, 7), 2, 3, 2, 3, 6)]
    with pytest.raises(ValidationError) as e:
        validate_response(DAILY, body(rows), w)
    assert e.value.code == "out_of_window"


def test_wrong_epoch_base_detected():
    b = body(bars())
    b["timestamp"] = [t + 315532800 for t in b["timestamp"]]   # 1980-based epoch read as 1970-based
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, b, WIN_I)
    assert e.value.code == "out_of_window"


def test_milliseconds_epoch_detected():
    b = body(bars())
    b["timestamp"] = [t * 1000 for t in b["timestamp"]]
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, b, WIN_I)
    assert e.value.code in ("timestamp_conversion", "out_of_window")


def test_timezone_shift_detected_as_systematic():
    b = body(bars(n=25))
    b["timestamp"] = [t - 19800 for t in b["timestamp"]]       # IST read as UTC -> bars at 03:45...
    w = Window(datetime(2024, 5, 1), datetime(2024, 5, 31))
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, b, w)
    assert e.value.code == "systematic_anomalies"


def test_duplicate_timestamps_rejected():
    b = body(bars())
    b["timestamp"][1] = b["timestamp"][0]
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, b, WIN_I)
    assert e.value.code == "duplicate_timestamps"


def test_isolated_ohlc_violation_is_kept_and_flagged_not_dropped():
    ok = []
    for day in (date(2024, 5, 6), date(2024, 5, 7)):            # 2 sessions x 25 bars = 50 rows
        start = datetime.combine(day, time(9, 15))
        ok += [(start + timedelta(minutes=15 * i), 100, 101, 99, 100, 5) for i in range(25)]
    ok[7] = (ok[7][0], 100, 99, 99.5, 100, 5)                  # high < open/close
    v = validate_response(INTRADAY_15, body(ok), Window(datetime(2024, 5, 1), datetime(2024, 5, 31)))
    assert v.n == 50
    assert list(v.anomalies["kind"]) == ["ohlc_relation"]


def test_special_saturday_session_is_kept_and_flagged():
    rows = [(datetime(2024, 5, 4), 1, 2, 1, 2, 5)]              # a Saturday
    weekdays = [datetime(2024, 5, 6) + timedelta(days=i) for i in range(60)]
    rows += [(d, 1, 2, 1, 2, 5) for d in weekdays if d.weekday() < 5][:30]
    v = validate_response(DAILY, body(rows), Window(datetime(2024, 5, 1), datetime(2024, 7, 30)))
    assert v.n == 31 and list(v.anomalies["kind"]) == ["weekend_candle"]


def test_intraday_off_grid_and_outside_session_flagged_when_isolated():
    rows = [(datetime(2024, 5, 6, 9, 15) + timedelta(minutes=15 * i), 100, 101, 99, 100, 5) for i in range(25)]
    rows.append((datetime(2024, 5, 6, 18, 15), 100, 101, 99, 100, 5))   # muhurat-style evening bar
    v = validate_response(INTRADAY_15, body(rows), WIN_I)
    assert v.n == 26 and list(v.anomalies["kind"]) == ["outside_session"]
