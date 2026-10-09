"""CLI wiring, selfcheck and verify-api, run against the synthetic local server."""
import json
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
