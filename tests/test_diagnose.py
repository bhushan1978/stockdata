"""The read-only diagnosis of what a provider returns for one instrument (repeated dates, fractional volumes, 'no data')."""
import json
from datetime import date, datetime, time, timedelta

import pytest

from conftest import FIXTURES
from fake_dhan import IST
from stockdata import diagnose
from stockdata.cli import main
from stockdata.providers.errors import PermanentError
from stockdata.timeframes import DAILY
from stockdata.validation import ValidationError, validate_response
from stockdata.windows import Window


def epoch(d: date, hh=0, mm=0) -> int:
    return int(datetime.combine(d, time(hh, mm)).replace(tzinfo=IST).timestamp())


def days(n, start=date(2024, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def body(rows):
    """rows: (date, open, high, low, close, volume, hh, mm)"""
    return {"open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows], "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows], "timestamp": [epoch(r[0], *(r[6:8] if len(r) > 6 else (0, 0))) for r in rows]}


def series(ds, base=100.0, factor=1.0, vol=1000, hh=0):
    return [(d, base * factor + i, base * factor + i + 1, base * factor + i - 1, base * factor + i, int(vol / factor), hh, 0) for i, d in enumerate(ds)]


def test_a_clean_response_is_reported_as_clean():
    a = diagnose.analyze_body(DAILY, body(series(days(30))))
    assert a["rows"] == 30 and a["unique_keys"] == 30 and "duplicates" not in a and "fractional_volume" not in a
    assert diagnose.verdict({"window": ["a", "b"], "requests": 1, "analysis": a}) == ["nothing unusual: the response is clean (no repeated dates, whole-number volumes)"]


def test_two_blocks_with_a_constant_ratio_look_like_an_adjusted_and_an_unadjusted_series():
    ds = days(40)
    b = body(series(ds, 200.0) + series(ds, 200.0, factor=0.5))             # the whole history twice: second block at half the price, twice the volume
    a = diagnose.analyze_body(DAILY, b)
    d = a["duplicates"]
    assert d["keys_repeated"] == 40 and d["conflicting"] == 40 and d["identical"] == 0 and d["max_copies"] == 2
    assert d["adjacent_copies_share"] == 0 and d["median_row_gap_between_copies"] == 40
    r = d["close_ratio_second_over_first"]
    assert 0.45 < r["median"] < 0.55 and r["share_within_1pct_of_median"] > 0.0 and d["volume_ratio_second_over_first_median"] == 2.0
    text = " | ".join(diagnose.verdict({"window": ["a", "b"], "requests": 1, "analysis": a}))
    assert "40 date(s) appear more than once" in text and "two blocks, not interleaved" in text
    assert len(d["samples"]) == 3 and d["samples"][0]["first"]["c"] != d["samples"][0]["second"]["c"]


def test_interleaved_copies_with_unrelated_prices_are_not_called_a_constant_factor():
    ds = days(30)
    rows = []
    for i, d in enumerate(ds):
        rows += [(d, 100 + i, 101 + i, 99 + i, 100 + i, 1000, 0, 0), (d, 300 + 7 * i, 301 + 7 * i, 299 + 7 * i, 300 + 7 * i, 500, 0, 0)]
    a = diagnose.analyze_body(DAILY, body(rows))
    assert a["duplicates"]["adjacent_copies_share"] == 1.0
    text = " | ".join(diagnose.verdict({"window": ["a", "b"], "requests": 1, "analysis": a}))
    assert "next to each other" in text and "two different price histories" in text


def test_identical_repeats_and_fractional_volumes_are_counted():
    ds = days(10)
    rows = series(ds) + series(ds[:3])                                       # the first three dates repeated exactly
    rows[5] = rows[5][:5] + (1234.5,) + rows[5][6:]
    a = diagnose.analyze_body(DAILY, body(rows))
    assert a["duplicates"]["identical"] == 3 and a["duplicates"]["conflicting"] == 0
    f = a["fractional_volume"]
    assert f["candles"] == 1 and f["samples"] == [1234.5] and 0 < f["rounding_would_change_total_by"] < 0.001


def test_malformed_and_empty_responses_are_described_not_raised():
    assert "six candle arrays" in diagnose.analyze_body(DAILY, {"open": []})["problem"]
    assert diagnose.analyze_body(DAILY, {k: [] for k in ("open", "high", "low", "close", "volume", "timestamp")})["rows"] == 0
    bad = {k: [1.0] for k in ("open", "high", "low", "close", "volume")} | {"timestamp": [None]}
    assert "not numbers" in diagnose.analyze_body(DAILY, bad)["problem"]


def test_a_problem_found_in_the_whole_history_request_is_probed_with_small_windows():
    ds = days(300)
    calls = []

    def fetch(start, end):
        calls.append((start, end))
        if (end - start).days > 100:                                           # the long request is the one that is doubled
            return body(series(ds) + series(ds, factor=0.5))
        return body(series([d for d in ds if start.date() <= d < end.date()]))

    report = diagnose.run_diagnosis(fetch, DAILY, datetime(2024, 1, 1), datetime(2025, 3, 1))
    assert report["requests"] == len(calls) == 3
    assert set(report["probes"]) == {"around_the_first_repeated_date", "most_recent_days"}
    text = " | ".join(diagnose.verdict(report))
    assert "a small window around the first repeated date" in text and "no repeated dates" in text
    assert diagnose.render("TEST", 1, {k: v for k, v in report.items() if k != "raw"}).startswith("== TEST (1)")


def test_a_clean_response_costs_exactly_one_request():
    calls = []
    report = diagnose.run_diagnosis(lambda s, e: calls.append(1) or body(series(days(20))), DAILY, datetime(2024, 1, 1), datetime(2024, 6, 1))
    assert report["requests"] == len(calls) == 1 and "probes" not in report


def test_a_no_data_answer_is_followed_by_one_probe_of_the_recent_days():
    def fetch(start, end):
        if (end - start).days > 100:
            raise PermanentError("HTTP 400 DH-907: no data", http_status=400, error_code="DH-907")
        return body(series(days(5)))

    report = diagnose.run_diagnosis(fetch, DAILY, datetime(1990, 1, 1), datetime(2026, 10, 11))
    assert report["requests"] == 2 and report["error"]["kind"] == "rejected"
    text = " | ".join(diagnose.verdict(report))
    assert "HAS data" in text and "DH-907" in text

    def nothing(start, end):
        raise PermanentError("HTTP 400 DH-907: no data", http_status=400, error_code="DH-907")

    text = " | ".join(diagnose.verdict(diagnose.run_diagnosis(nothing, DAILY, datetime(1990, 1, 1), datetime(2026, 10, 11))))
    assert "no data for this instrument at all" in text


def test_the_validators_refusal_says_what_it_found_and_points_to_the_diagnosis():
    ds = days(30)
    win = Window(datetime(2024, 1, 1), datetime(2025, 1, 1))
    with pytest.raises(ValidationError) as e:
        validate_response(DAILY, body(series(ds) + series(ds, factor=0.5)), win)
    msg = e.value.message
    assert e.value.code == "duplicate_timestamps" and "30 dates carry different candles" in msg and "overlapping series" in msg
    assert "from 2024-01-01 to 2024-02-09" in msg and "ratio median 0.5" in msg and "diagnose" in msg and len(msg) < 400


def test_the_command_stores_nothing_saves_the_raw_response_and_costs_one_request_for_a_clean_instrument(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("DHAN_BASE_URL", fake.base_url)
    monkeypatch.setenv("STOCKDATA_RPS", "1000")
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    data = tmp_path / "data"
    run = lambda *a: main(["--data-dir", str(data), *a])                      # noqa: E731
    assert run("instruments", "load", "--file", str(FIXTURES / "master_compact_synthetic.csv")) == 0
    capsys.readouterr()
    n0 = len(fake.log)
    assert run("diagnose", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-05-01", "--end-date", "2024-06-03") == 0
    out = capsys.readouterr().out
    assert "nothing is stored" in out and "== SIMALPHA (900001)" in out and "nothing unusual" in out and len(fake.log) - n0 == 1
    saved = list((data / "diagnostics").glob("SIMALPHA_daily_*.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text())["response"]["timestamp"]
    import duckdb
    con = duckdb.connect(str(data / "stockdata_daily.duckdb"), read_only=True)
    assert con.execute("SELECT count(*) FROM candles_daily").fetchone()[0] == 0 and con.execute("SELECT count(*) FROM download_windows").fetchone()[0] == 0
    con.close()
    with pytest.raises(SystemExit):
        run("diagnose", "daily", "--symbols", "NOSUCH")
    with pytest.raises(SystemExit):
        run("diagnose", "daily", "--symbols", ",".join(f"S{i}" for i in range(11)))
