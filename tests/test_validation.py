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
    ("volume", None, "non_numeric_volume"),
    ("volume", 12.5, "non_integer_volume"), ("timestamp", None, "non_numeric_timestamp")])
def test_bad_values_rejected(field, value, code):
    b = body(bars())
    b[field][2] = value
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, b, WIN_I)
    assert e.value.code == code


def test_negative_volume_is_kept_as_received_and_flagged_not_discarded():
    """Seen live: a bar with volume -35,067,964 in a special Saturday session. One bad bar must not
    discard 89 days of valid data, and the value must be stored exactly as Dhan sent it."""
    session = [(datetime(2024, 5, 6, 9, 15) + timedelta(minutes=15 * i), 100, 101, 99, 100, 5) for i in range(25)]
    session[7] = (session[7][0], 100, 101, 99, 100, -35_067_964)
    v = validate_response(INTRADAY_15, body(session), WIN_I)
    assert v.n == 25 and int(v.candles["volume"].iloc[7]) == -35_067_964
    assert list(v.anomalies["kind"]) == ["negative_volume"]
    assert "-35067964" in v.anomalies["detail"].iloc[0]


def test_widespread_negative_volume_is_a_systematic_field_problem():
    session = [(datetime(2024, 5, 6, 9, 15) + timedelta(minutes=15 * i), 100, 101, 99, 100, -5) for i in range(25)]
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, body(session), WIN_I)
    assert e.value.code == "systematic_anomalies"


def test_special_weekend_sessions_are_flagged_but_never_make_a_window_systematic():
    rows = []
    for day in (date(2024, 5, 6), date(2024, 5, 7), date(2024, 5, 8), date(2024, 5, 9)):          # 4 weekday sessions
        rows += [(datetime.combine(day, time(9, 15)) + timedelta(minutes=15 * i), 100, 101, 99, 100, 5) for i in range(25)]
    for day in (date(2024, 5, 4), date(2024, 5, 11)):                                              # 2 Saturday sessions
        rows += [(datetime.combine(day, time(9, 15)) + timedelta(minutes=15 * i), 100, 101, 99, 100, 5) for i in range(25)]
    v = validate_response(INTRADAY_15, body(rows), WIN_I)       # 50 of 150 bars (33%) are weekend: still accepted
    assert v.n == 150
    assert set(v.anomalies["kind"]) == {"weekend_candle"} and v.anomalies["candle_key"].nunique() == 50


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


def test_identical_duplicate_candles_are_collapsed_to_one_and_flagged():
    b = body(bars())
    b["timestamp"][1] = b["timestamp"][0]
    b["volume"][1] = b["volume"][0]                                   # the same candle twice, byte for byte
    v = validate_response(INTRADAY_15, b, WIN_I)
    assert v.n == 4 and list(v.candles["candle_timestamp"].dt.strftime("%H:%M")) == ["09:15", "09:45", "10:00", "10:15"]
    note = v.anomalies[v.anomalies["kind"] == "duplicate_dropped"]
    assert len(note) == 1 and "identical copies" in note["detail"].iloc[0]


def test_different_candles_for_one_key_are_not_stored_nothing_is_merged_and_the_rest_of_the_response_is():
    """Seen live (WELSPUNLIV daily, 1990 to today): one repeated date must not discard 36 years of candles."""
    rows = [(datetime(2024, 5, d), 100 + d, 110 + d, 90 + d, 105 + d, 1000 + d) for d in (6, 7, 8, 9, 10)]
    b = body(rows)
    for k, val in zip(("open", "high", "low", "close", "volume", "timestamp"), (500, 510, 490, 505, 7, epoch(datetime(2024, 5, 7, 18, 15)))):
        b[k].append(val)                                              # a second, different 7 May candle (evening timestamp)
    v = validate_response(DAILY, b, WIN_D)
    assert [d.date() for d in v.candles["candle_date"]] == [date(2024, 5, 6), date(2024, 5, 8), date(2024, 5, 9), date(2024, 5, 10)]
    conflict = v.anomalies[v.anomalies["kind"] == "duplicate_conflict"]
    assert len(conflict) == 1 and conflict["candle_key"].iloc[0] == datetime(2024, 5, 7)
    detail = conflict["detail"].iloc[0]
    assert "none stored" in detail and "o=107" in detail and "o=500" in detail and "v=1007" in detail and "v=7" in detail   # both raw rows kept for review
    assert not (v.candles["open"] == 500).any() and not (v.candles["open"] == 107).any()        # neither was picked or blended


def test_many_conflicting_keys_still_reject_the_whole_response_as_a_time_base_problem():
    rows = [(datetime(2024, 5, d), 100 + d, 110 + d, 90 + d, 105 + d, 1000 + d) for d in range(1, 15)]
    b = body(rows + [(datetime(2024, 5, d, 18, 15), 1, 2, 1, 2, 9) for d in range(1, 8)])   # 7 days with a second, different candle
    with pytest.raises(ValidationError) as e:
        validate_response(DAILY, b, WIN_D)
    assert e.value.code == "duplicate_timestamps" and "nothing was stored" in e.value.message


def test_a_response_that_is_only_a_conflicting_pair_is_rejected_not_emptied():
    b = body([(datetime(2024, 5, 7), 1, 2, 1, 2, 5), (datetime(2024, 5, 7, 18, 15), 3, 4, 3, 4, 6)])
    with pytest.raises(ValidationError) as e:
        validate_response(DAILY, b, WIN_D)
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


def test_a_1530_bar_is_a_flagged_post_close_bar_and_does_not_count_as_systematic():
    """Seen live: many real sessions carry a bar starting 15:30. One per 26-bar session (3.8%) sits just under
    the systematic cutoff, so it must not be counted toward it at all."""
    rows = []
    for day in (date(2024, 5, 6), date(2024, 5, 7), date(2024, 5, 8)):
        rows += [(datetime.combine(day, time(9, 15)) + timedelta(minutes=15 * i), 100, 101, 99, 100, 5) for i in range(26)]
    rows[7] = (rows[7][0], 100, 99, 99.5, 100, 5)                       # plus one unrelated OHLC oddity
    rows[33] = (rows[33][0], 100, 99, 99.5, 100, 5)
    v = validate_response(INTRADAY_15, body(rows), WIN_I)
    assert v.n == 78
    kinds = v.anomalies["kind"].value_counts().to_dict()
    assert kinds == {"post_close_bar": 3, "ohlc_relation": 2}


def test_a_time_base_shift_is_still_caught_even_though_1530_is_tolerated():
    rows = []
    for day in (date(2024, 5, 6), date(2024, 5, 7)):
        rows += [(datetime.combine(day, time(9, 15)) + timedelta(minutes=15 * i) + timedelta(hours=5, minutes=30),
                  100, 101, 99, 100, 5) for i in range(25)]             # IST read as UTC: bars land 14:45 .. 21:00
    with pytest.raises(ValidationError) as e:
        validate_response(INTRADAY_15, body(rows), WIN_I)
    assert e.value.code == "systematic_anomalies"


def test_a_response_with_a_conflicting_duplicate_commits_cleanly_and_the_integrity_checks_still_pass(tmp_path):
    from stockdata.db import CandleDB
    from stockdata.instruments import Instrument
    from stockdata.reports import integrity_checks
    rows = [(datetime(2024, 5, d), 100 + d, 110 + d, 90 + d, 105 + d, 1000 + d) for d in (6, 7, 8, 9, 10)]
    b = body(rows)
    for k, val in zip(("open", "high", "low", "close", "volume", "timestamp"), (500, 510, 490, 505, 7, epoch(datetime(2024, 5, 7, 18, 15)))):
        b[k].append(val)
    inst = Instrument("NSE_EQ", 900001, "SIMALPHA", "EQUITY")
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        v = validate_response(DAILY, b, WIN_D)
        db.commit_window(inst, WIN_D, candles=v.candles, anomalies=v.anomalies, zero_volume=v.zero_volume_rows, attempts=1, http_status=200)
        assert db.con.execute("SELECT count(*) FROM candles_daily").fetchone()[0] == 4
        kinds = dict(db.con.execute("SELECT kind, count(*) FROM data_anomalies GROUP BY 1").fetchall())
        assert kinds == {"duplicate_conflict": 1}
        assert db.con.execute("SELECT status FROM download_windows").fetchone()[0] == "complete"
        assert all(c.ok for c in integrity_checks(db))


def _daily_rows(volumes, close=100.0):
    return [(datetime(2024, 5, 6) + timedelta(days=i), close, close + 1, close - 1, close, v) for i, v in enumerate(volumes)]


def test_fractional_volume_that_is_immaterial_is_rounded_and_each_candle_is_flagged_with_the_received_value():
    """Seen live (SANOFI daily): 85% of candles had volumes like 22355.1; rounding changed the total by 0.0011%."""
    v = validate_response(DAILY, body(_daily_rows([22355.1, 194594.4, 85410.15, 1_000_000, 700_000])), WIN_D)
    assert list(v.candles["volume"]) == [22355, 194594, 85410, 1_000_000, 700_000] and str(v.candles["volume"].dtype) == "int64"
    flags = v.anomalies[v.anomalies["kind"] == "volume_rounded"]
    assert list(flags["candle_key"].dt.day) == [6, 7, 8]                    # only the candles that were changed
    assert flags["detail"].iloc[0] == "volume 22355.1 stored as 22355" and "194594.4" in flags["detail"].iloc[1]
    assert not v.anomalies["kind"].isin(["duplicate_dropped", "duplicate_conflict"]).any()


def test_fractional_volume_that_would_change_the_total_materially_is_still_refused():
    with pytest.raises(ValidationError) as e:
        validate_response(DAILY, body(_daily_rows([2.5, 3.4, 1.6, 4.5, 2.5])), WIN_D)          # tiny volumes: rounding moves the total by >10%
    assert e.value.code == "non_integer_volume" and "would change the total volume by" in e.value.message and "nothing was stored" in e.value.message


def test_integer_valued_float_volumes_are_not_flagged():
    v = validate_response(DAILY, body(_daily_rows([1000.0, 2000.0, 3000.0])), WIN_D)
    assert list(v.candles["volume"]) == [1000, 2000, 3000] and v.anomalies.empty


def test_the_rounding_flag_follows_only_candles_that_are_stored_not_a_dropped_duplicate_copy():
    rows = _daily_rows([1_000_000, 2_000_000.4, 3_000_000, 4_000_000])
    dup = rows[1]
    b = body(rows + [dup])                                                    # byte-identical second copy of the fractional candle
    v = validate_response(DAILY, b, WIN_D)
    assert v.n == 4 and sorted(v.anomalies["kind"]) == ["duplicate_dropped", "volume_rounded"]
    assert list(v.anomalies[v.anomalies["kind"] == "volume_rounded"]["candle_key"]) == [datetime(2024, 5, 7)]


def test_a_response_with_rounded_volumes_commits_cleanly_and_the_integrity_checks_still_pass(tmp_path):
    from stockdata.db import CandleDB
    from stockdata.instruments import Instrument
    from stockdata.reports import integrity_checks
    inst = Instrument("NSE_EQ", 900002, "SIMVOL", "EQUITY")
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        v = validate_response(DAILY, body(_daily_rows([22355.1, 194594.4, 1_000_000, 700_000])), WIN_D)
        db.commit_window(inst, WIN_D, candles=v.candles, anomalies=v.anomalies, zero_volume=v.zero_volume_rows, attempts=1, http_status=200)
        assert db.con.execute("SELECT volume FROM candles_daily ORDER BY candle_date").fetchall() == [(22355,), (194594,), (1_000_000,), (700_000,)]
        assert dict(db.con.execute("SELECT kind, count(*) FROM data_anomalies GROUP BY 1").fetchall()) == {"volume_rounded": 2}
        assert db.con.execute("SELECT status FROM download_windows").fetchone()[0] == "complete"
        assert all(c.ok for c in integrity_checks(db))
