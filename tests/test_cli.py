"""CLI wiring, selfcheck and verify-api, run against the synthetic local server."""
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from conftest import FIXTURES
from stockdata.cli import main

MASTER = str(FIXTURES / "master_compact_synthetic.csv")


@pytest.fixture
def cli(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("DHAN_BASE_URL", fake.base_url)
    monkeypatch.setenv("STOCKDATA_RPS", "1000")
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    data = tmp_path / "data"

    def run(*argv):
        return main(["--data-dir", str(data), *argv])

    run.data = data
    return run


def test_load_universe_into_both_databases_then_download_report_status(cli, fake, capsys):
    assert cli("instruments", "summary", "--file", MASTER) == 0
    out = capsys.readouterr().out
    assert "NSE | E | EQUITY" in out and "-> 3 instruments" in out
    assert cli("instruments", "load", "--file", MASTER) == 0
    assert (cli.data / "stockdata_daily.duckdb").exists() and (cli.data / "stockdata_15min.duckdb").exists()
    assert cli("download", "daily", "--symbols", "SIMALPHA,SIMBETA", "--start-date", "2024-05-01") == 0
    assert cli("download", "15min", "--symbols", "SIMALPHA", "--start-date", "2024-05-01") == 0
    assert {r["path"].split("/")[-1] for r in fake.log} == {"historical", "intraday"}
    capsys.readouterr()
    assert cli("report", "daily", "--csv") == 0
    rep = capsys.readouterr().out
    assert "SIMALPHA" in rep and "[PASS] unique key" in rep and "FAIL" not in rep
    assert list((cli.data / "reports").glob("coverage_daily_*.csv"))
    assert cli("status", "15min") == 0
    assert "candles:" in capsys.readouterr().out


def test_a_download_needs_an_explicit_selection_and_confirmation(cli, fake):
    cli("instruments", "load", "--file", MASTER)
    with pytest.raises(SystemExit):
        cli("download", "daily")                                           # nothing selected
    with pytest.raises(SystemExit) as e:
        cli("download", "daily", "--all")                                  # --all alone is refused
    assert "--confirm-full-download" in str(e.value)
    assert fake.log == []                                                  # nothing was sent


def test_dry_run_plans_but_sends_nothing_and_needs_no_token(cli, fake, monkeypatch, capsys):
    cli("instruments", "load", "--file", MASTER)
    monkeypatch.delenv("DHAN_ACCESS_TOKEN")
    assert cli("download", "15min", "--all", "--dry-run", "--start-date", "2024-01-01", "--end-date", "2024-06-03") == 0
    out = capsys.readouterr().out
    assert "3 instrument(s)" in out and "6 request(s)" in out              # 154 days -> 2 windows x 3 instruments
    assert fake.log == []


def test_missing_token_stops_before_any_request(cli, fake, monkeypatch):
    cli("instruments", "load", "--file", MASTER)
    monkeypatch.delenv("DHAN_ACCESS_TOKEN")
    with pytest.raises(SystemExit) as e:
        cli("download", "daily", "--symbols", "SIMALPHA")
    assert "DHAN_ACCESS_TOKEN" in str(e.value) and fake.log == []


def test_end_date_cannot_reach_into_unfinished_days(cli):
    cli("instruments", "load", "--file", MASTER)
    with pytest.raises(SystemExit) as e:
        cli("download", "daily", "--symbols", "SIMALPHA", "--end-date", "2999-01-01")
    assert "not final" in str(e.value)


def test_window_cap_is_enforced_on_the_command_line(cli):
    cli("instruments", "load", "--file", MASTER)
    with pytest.raises(SystemExit) as e:
        cli("download", "15min", "--symbols", "SIMALPHA", "--window-days", "91", "--start-date", "2024-05-01")
    assert "90-day cap" in str(e.value)


def test_interrupted_run_resumes_by_repeating_the_same_command(cli, fake, monkeypatch):
    cli("instruments", "load", "--file", MASTER)
    fake.fail(times=10_000, status=503, match=lambda p: p["securityId"] == "900002")
    code = cli("download", "daily", "--symbols", "SIMALPHA,SIMBETA,SIMGAMMA", "--start-date", "2024-01-01",
               "--max-attempts", "2")
    assert code == 2                                                       # completed_with_errors
    fake.rules.clear()
    n = len(fake.log)
    assert cli("download", "daily", "--symbols", "SIMALPHA,SIMBETA,SIMGAMMA", "--start-date", "2024-01-01") == 0
    assert {r["payload"]["securityId"] for r in fake.log[n:]} == {"900002"}


def test_selfcheck_end_to_end_passes_and_is_isolated_from_the_real_databases(cli, fake, capsys):
    code = cli("selfcheck", "--file", MASTER, "--symbols", "SIMALPHA,SIMBETA,SIMGAMMA",
               "--daily-days", "120", "--intraday-days", "60")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "FAIL" not in out
    for needle in ("second run requested nothing", "forced re-download adds no rows", "the other database file was not modified",
                   "no request reached past the exclusive cutoff", "every request has interval=15"):
        assert needle in out
    assert (cli.data / "selfcheck" / "stockdata_daily.duckdb").exists()
    assert not (cli.data / "stockdata_daily.duckdb").exists()              # production paths untouched
    report = json.loads(next((cli.data / "selfcheck" / "reports").glob("selfcheck_*.json")).read_text())
    assert all(r["ok"] for r in report["results"])


def test_selfcheck_with_a_partial_selection_of_a_larger_loaded_universe(cli, fake, capsys):
    """Regression: the real universe holds thousands of instruments, only a few of which are downloaded
    (their candle counts are NA in the coverage table)."""
    code = cli("selfcheck", "--file", MASTER, "--symbols", "SIMALPHA,SIMBETA", "--daily-days", "90", "--intraday-days", "45")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "SIMALPHA=" in out and "SIMBETA=" in out and "SIMGAMMA" not in out


def _verify(cli, capsys, sec=900001):
    assert cli("verify-api", "--symbol", "SIMALPHA", "--security-id", str(sec)) == 0
    return capsys.readouterr().out


def test_verify_api_confirms_assumptions_when_the_server_behaves(cli, fake, capsys):
    out = _verify(cli, capsys)
    assert "epoch base: unix_1970" in out and "CONFIRMED" in out
    assert "toDate boundary: EXCLUSIVE" in out
    assert "documented cap of 90 days is CONFIRMED" in out
    assert "first bar 09:15" in out and "NOT CONFIRMED" not in out.split("[intraday] span")[0]
    assert list((cli.data / "reports").glob("verify_api_*.json"))


def test_verify_api_detects_an_inclusive_to_date(cli, fake, capsys):
    fake.inclusive_to = True
    assert "INCLUSIVE" in _verify(cli, capsys)


def test_verify_api_detects_the_old_1980_epoch_base(cli, fake, capsys):
    fake.epoch_offset = 315532800
    assert "epoch base: base_1980" in _verify(cli, capsys)


def test_verify_api_reports_how_an_empty_window_is_signalled(cli, fake, capsys):
    fake.empty_error_code = "DH-907"
    out = _verify(cli, capsys)
    assert "a window with no data returns: ok=False" in out and "DH-907" in out


# ------------------------------------------------------------ request limits through the CLI
def test_download_stops_at_the_run_limit_with_exit_code_4_and_a_rerun_resumes(cli, fake, capsys):
    cli("instruments", "load", "--file", MASTER)
    args = ("download", "daily", "--symbols", "SIMALPHA,SIMBETA,SIMGAMMA", "--start-date", "2024-01-01")
    code = cli(*args, "--max-requests-per-run", "2")
    out = capsys.readouterr().out
    assert code == 4 and len(fake.log) == 2
    assert "per-run request limit reached" in out and "rerun the same command to resume" in out
    assert "other applications using the same dhan account are not included" in out.lower()
    assert cli(*args) == 0 and len(fake.log) == 3                       # only the one unfinished instrument is requested
    capsys.readouterr()
    assert cli("usage") == 0
    usage = capsys.readouterr().out
    assert "used today 3" in usage and "daily=3" in usage and "NOT included" in usage and "80,000" in usage


def test_invalid_request_limits_are_refused_before_anything_is_sent(cli, fake, monkeypatch):
    cli("instruments", "load", "--file", MASTER)
    with pytest.raises(SystemExit) as e:
        cli("download", "daily", "--symbols", "SIMALPHA", "--max-requests-per-day", "100001")
    assert "100,000" in str(e.value) and fake.log == []
    monkeypatch.setenv("STOCKDATA_MAX_REQUESTS_PER_DAY", "250000")
    with pytest.raises(ValueError):
        cli("usage")


@pytest.mark.parametrize("flag", ["--max-requests-per-run", "--max-requests-per-day"])
def test_a_zero_or_negative_limit_is_rejected_not_silently_replaced_by_the_default(cli, fake, flag):
    cli("instruments", "load", "--file", MASTER)
    for bad in ("0", "-5"):
        with pytest.raises(SystemExit) as e:
            cli("download", "daily", "--symbols", "SIMALPHA", flag, bad)
        assert "positive" in str(e.value)
    assert fake.log == []


def test_a_dry_run_says_whether_the_plan_fits_the_limits(cli, fake, capsys):
    cli("instruments", "load", "--file", MASTER)
    cli("download", "15min", "--all", "--dry-run", "--start-date", "2024-01-01", "--end-date", "2024-06-03",
        "--max-requests-per-run", "3")
    out = capsys.readouterr().out
    assert "this plan needs 6" in out and "only 3 fit now" in out and fake.log == []


def test_selfcheck_and_verify_api_stop_cleanly_at_a_limit(cli, fake, capsys):
    assert cli("selfcheck", "--file", MASTER, "--symbols", "SIMALPHA", "--max-requests-per-run", "1") == 4
    out = capsys.readouterr().out
    assert "STOPPED at a request limit" in out and "NOT included" in out
    assert cli("verify-api", "--symbol", "SIMALPHA", "--security-id", "900001", "--max-requests-per-run", "2") == 4
    assert "STOPPED" in capsys.readouterr().out


# ------------------------------------------------------------------------ refresh through the CLI
def _daily_rows(cli_run, sec=900001):
    import duckdb
    con = duckdb.connect(str(cli_run.data / "stockdata_daily.duckdb"), read_only=True)
    try:
        return con.execute("SELECT candle_date, close FROM candles_daily WHERE security_id = ? ORDER BY 1", [sec]).fetchall()
    finally:
        con.close()


def test_refresh_end_to_end_replaces_revised_history_and_reports_it(cli, fake, capsys):
    cli("instruments", "load", "--file", MASTER)
    assert cli("download", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-01-01") == 0
    before = dict(_daily_rows(cli))
    sim = fake.instruments[900001]
    sim.split_on, sim.split_factor = date(2024, 3, 10), 0.5               # the source revises history retroactively
    capsys.readouterr()
    assert cli("refresh", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-01-01") == 0
    out = capsys.readouterr().out
    assert "no corporate-action factor is applied" in out and "independent split/bonus validation stays pending" in out
    import re
    assert int(re.search(r"changed (\d+)", out).group(1)) > 0              # the revised candles were counted
    after = dict(_daily_rows(cli))
    assert set(after) == set(before) and after != before
    assert after[date(2024, 1, 10)] == pytest.approx(before[date(2024, 1, 10)] * 0.5, rel=1e-3)
    assert after[date(2024, 5, 10)] == before[date(2024, 5, 10)]          # after the revision date: unchanged
    capsys.readouterr()
    cli("status", "daily")
    assert "refresh jobs: {'complete': 1}" in capsys.readouterr().out


def test_refresh_requires_explicit_symbols_and_a_sane_range(cli, fake):
    cli("instruments", "load", "--file", MASTER)
    cli("download", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-01-01")
    with pytest.raises(SystemExit) as e:
        cli("refresh", "daily", "--start-date", "2024-01-01")
    assert "explicit" in str(e.value)
    with pytest.raises(SystemExit):
        cli("refresh", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-02-01", "--end-date", "2024-01-01")
    with pytest.raises(SystemExit) as e:
        cli("refresh", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-01-01", "--end-date", "2999-01-01")
    assert "final day boundary" in str(e.value)
    with pytest.raises(SystemExit) as e:
        cli("refresh", "15min", "--symbols", "SIMALPHA", "--start-date", "2024-01-01", "--window-days", "91")
    assert "90-day cap" in str(e.value)                                    # (instruments load created both databases)


def test_refresh_dry_run_sends_nothing_and_changes_nothing(cli, fake, capsys):
    cli("instruments", "load", "--file", MASTER)
    cli("download", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-01-01")
    path = cli.data / "stockdata_daily.duckdb"
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    n = len(fake.log)
    capsys.readouterr()
    assert cli("refresh", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-01-01", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dry run: 1 request(s)" in out and "new job" in out and "FITS" in out
    assert len(fake.log) == n and hashlib.sha256(path.read_bytes()).hexdigest() == h


def test_refresh_exit_codes_for_held_empty_and_for_a_request_limit(cli, fake, capsys):
    cli("instruments", "load", "--file", MASTER)
    cli("download", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-01-01")
    before = _daily_rows(cli)
    fake.fail(times=10, status="empty")                                   # an ambiguous empty answer while candles are stored
    capsys.readouterr()
    assert cli("refresh", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-01-01") == 2
    out = capsys.readouterr().out
    assert "held" in out.lower() and "nothing was deleted" in out
    assert _daily_rows(cli) == before                                      # no candle was deleted
    fake.rules.clear()
    assert cli("refresh", "daily", "--symbols", "SIMALPHA", "--start-date", "2024-01-01", "--max-requests-per-run", "1") == 0
