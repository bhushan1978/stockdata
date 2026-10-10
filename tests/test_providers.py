"""Provider layer: capability reporting, authentication status, instrument mapping, request/response normalisation into the
canonical candle format for FYERS and Upstox (against local synthetic servers), range and rate-limit behaviour, provenance
and conflict handling, idempotency and secret redaction. No provider is contacted."""
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import CUTOFF, load_universe, make_client, make_opts
from fake_dhan import FakeDhan, SimInstrument
from fake_providers import FakeFyers, FakeUpstox
from stockdata.budget import RequestBudget, RequestLimitReached, UsageLedger, read_usage
from stockdata.db import CandleDB
from stockdata.downloader import Downloader, RunAborted
from stockdata.plan import PlanError, build_download_plan
from stockdata.providers import PROVIDER_NAMES, all_providers, get_provider, ledger_label, provider_of_label
from stockdata.providers.base import (DOCUMENTED, FACT_STATUSES, MultiWindowLimiter, PROBE_VERIFIED, RateLimit, RawResponse)
from stockdata.providers.errors import AuthError, PermanentError, TransientError
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.validation import empty_result
from stockdata.windows import Window
from web_helpers import fake_jwt

DETAILED = "master_detailed_synthetic.csv"
SYMS = ("SIMALPHA", "SIMBETA", "SIMGAMMA")
STUB = SimpleNamespace(rps_override=None, rps=3.0)
FAST = lambda: MultiWindowLimiter([(10_000, 1.0)], sleep=lambda s: None)       # noqa: E731


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


@pytest.fixture
def fyers():
    s = sims()
    server = FakeFyers({f"NSE:SIM{n}-EQ": s[i] for n, i in (("ALPHA", 900001), ("BETA", 900002), ("GAMMA", 900003))})
    server.url = server.start()
    yield server
    server.stop()


@pytest.fixture
def upstox():
    s = sims()
    server = FakeUpstox({**{f"INE000SIM00{k}": s[900000 + k] for k in (1, 2, 3)}, "INE002A01018": s[900001]})   # + the connection-test key
    server.url = server.start()
    yield server
    server.stop()


def env_for(name, server=None):
    if name == "fyers":
        return {"FYERS_APP_ID": "TEST-100", "FYERS_ACCESS_TOKEN": "fyers-test-token", "FYERS_BASE_URL": server.url}
    return {"UPSTOX_ANALYTICS_TOKEN": "upstox-test-token", "UPSTOX_BASE_URL": server.url}


def new_db(tmp_path, tf, name="db.duckdb"):
    db = CandleDB(tf, tmp_path / name)
    load_universe(db, DETAILED)
    return db


def run_provider(db, name, server, tf, symbols=SYMS, budget=None, window_days=None, **opt_kw):
    adapter = get_provider(name)
    client = adapter.make_client(env_for(name, server), settings=STUB, label=tf.name, budget=budget, limiter=FAST())
    days = window_days or (adapter.window_days(tf) if tf.is_intraday else 365)
    opts = make_opts(tf, window_days=days, provider=name, **opt_kw)
    stats = Downloader(db, client, provider=adapter).run(db.get_instruments(symbols=list(symbols)), opts)
    return stats, client


def candles(db, tf):
    k = tf.key_col
    return db.con.execute(f"SELECT security_id, {k}, open, high, low, close, volume FROM {tf.table} ORDER BY 1, 2").fetchall()


# ------------------------------------------------------------------------------------- capabilities
def test_every_provider_reports_capabilities_with_an_honest_status_for_each_fact():
    assert PROVIDER_NAMES == ("dhan", "fyers", "upstox")
    for adapter in all_providers():
        caps = adapter.capabilities()
        json.dumps(caps.to_dict())                                   # the interface can show all of it
        assert set(caps.timeframes) == {"daily", "15min"}
        for tfs in caps.timeframes.values():
            assert tfs.status in FACT_STATUSES
        assert caps.rate_limit.status in FACT_STATUSES and caps.adjustment_status in FACT_STATUSES
        assert "terms" in caps.terms_note.lower()                    # the 'subject to the provider's terms' notice travels with it
        assert caps.auth.unattended_note and caps.auth.how_to_obtain
    f, u, d = (get_provider(n).capabilities() for n in ("fyers", "upstox", "dhan"))
    # nothing is presented as verified unless this project verified it live
    assert d.timeframes["daily"].status == PROBE_VERIFIED
    assert f.timeframes["15min"].status == DOCUMENTED and f.timeframes["15min"].earliest_date is None
    assert u.timeframes["15min"].earliest_date == date(2022, 1, 1)    # fewer than five years of 15-minute history
    assert f.auth.interactive_login and not u.auth.interactive_login
    assert "NOT claimed as verified" in u.auth.unattended_note or "not claimed" in u.auth.unattended_note.lower()
    assert u.rate_limit.sustained_per_second(0.6) < f.rate_limit.sustained_per_second(0.6)   # Upstox's 30-minute window binds


def test_the_limiter_enforces_every_window_at_once():
    now = [0.0]
    slept = []
    lim = MultiWindowLimiter([(2, 1.0), (3, 10.0)], clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    for _ in range(3):
        lim.wait()
    assert sum(slept) >= 1.0 - 1e-9                                  # 3 requests need > 1s under '2 per second'
    lim.wait()                                                        # the 4th must wait for the 10-second window
    assert now[0] >= 10.0 - 1e-9


# ------------------------------------------------------------------------------------ credentials
def test_credential_status_reports_missing_expired_and_expiring_without_contacting_anyone():
    f, u = get_provider("fyers"), get_provider("upstox")
    assert f.credential_status({}).state == "not_configured" and f.credential_status({}).missing == ("FYERS_APP_ID", "FYERS_ACCESS_TOKEN")
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    past, soon, far = (int(now.timestamp()) + d for d in (-60, 3600, 400 * 86400))
    assert f.credential_status({"FYERS_APP_ID": "A-100", "FYERS_ACCESS_TOKEN": fake_jwt(past)}, now).state == "expired"
    assert f.credential_status({"FYERS_APP_ID": "A-100", "FYERS_ACCESS_TOKEN": fake_jwt(soon)}, now).state == "expiring_soon"
    assert u.credential_status({"UPSTOX_ANALYTICS_TOKEN": fake_jwt(far)}, now).state == "ready"
    assert u.credential_status({"UPSTOX_ACCESS_TOKEN": fake_jwt(past)}, now).state == "expired"      # the OAuth token also counts
    opaque = f.credential_status({"FYERS_APP_ID": "A-100", "FYERS_ACCESS_TOKEN": "opaque-token-without-expiry"}, now)
    assert opaque.state == "ready" and not opaque.expiry_known and "connection test" in opaque.message
    assert u.credential_status({}).state == "not_configured"
    secret = "fyers-secret-value-123456"
    assert secret not in json.dumps(f.credential_status({"FYERS_APP_ID": "A-100", "FYERS_ACCESS_TOKEN": secret}).to_dict())


def test_a_client_never_exposes_its_credential_and_refuses_a_plain_http_remote():
    adapter = get_provider("fyers")
    client = adapter.make_client({"FYERS_APP_ID": "A-100", "FYERS_ACCESS_TOKEN": "tok-secret-9999", "FYERS_BASE_URL": "http://127.0.0.1:1"},
                                 settings=STUB, label="daily", limiter=FAST())
    assert "tok-secret-9999" not in repr(client) and "A-100" not in repr(client)
    from stockdata.providers.errors import ProviderSetupError
    with pytest.raises(ProviderSetupError, match="non-HTTPS"):
        adapter.make_client({"FYERS_APP_ID": "A", "FYERS_ACCESS_TOKEN": "t", "FYERS_BASE_URL": "http://example.com"}, settings=STUB, label="daily")
    with pytest.raises(ProviderSetupError, match="FYERS credentials are not set"):
        adapter.make_client({}, settings=STUB, label="daily")
    with pytest.raises(ProviderSetupError, match="Upstox credentials are not set"):
        get_provider("upstox").make_client({}, settings=STUB, label="daily")


# --------------------------------------------------------------------------------------- mapping
def test_instrument_mapping_is_explicit_and_never_guessed(tmp_path):
    with new_db(tmp_path, DAILY) as db:
        alpha = db.get_instruments(symbols=["SIMALPHA"])[0]
        assert get_provider("dhan").map_instrument(alpha) == "900001"
        assert get_provider("fyers").map_instrument(alpha) == "NSE:SIMALPHA-EQ"
        assert get_provider("upstox").map_instrument(alpha) == "NSE_EQ|INE000SIM001"
        assert get_provider("upstox").map_instrument(type(alpha)("NSE_EQ", 1, "X", "EQUITY", isin="")) is None     # no ISIN: not guessed
        assert get_provider("fyers").map_instrument(type(alpha)("BSE_EQ", 1, "X", "EQUITY")) is None               # NSE equity only


def test_the_plan_skips_unmapped_instruments_instead_of_requesting_garbage(tmp_path):
    with CandleDB(DAILY, tmp_path / "compact.duckdb") as db:
        load_universe(db)                                              # the COMPACT master has no ISIN
        settings = SimpleNamespace(provider="dhan", intraday_date_format="datetime", client_id=None)
        plan = build_download_plan(db, settings=settings, symbols=list(SYMS), provider="upstox", start_date=date(2024, 1, 1),
                                   end_date=date(2024, 3, 1))
        assert plan.instruments == [] and len(plan.unmapped) == 3 and plan.requests == 0
        dhan_plan = build_download_plan(db, settings=settings, symbols=list(SYMS), provider="dhan", start_date=date(2024, 1, 1),
                                        end_date=date(2024, 3, 1))
        assert len(dhan_plan.instruments) == 3 and not dhan_plan.unmapped


# ----------------------------------------------------------------- normalisation into the canonical format
@pytest.mark.parametrize("tf", [DAILY, INTRADAY_15], ids=["daily", "15min"])
@pytest.mark.parametrize("name", ["fyers", "upstox"])
@pytest.mark.parametrize("inclusive", [True, False], ids=["to-inclusive", "to-exclusive"])
def test_other_providers_store_exactly_what_dhan_stores_for_the_same_bars(tmp_path, dhan, fyers, upstox, name, tf, inclusive):
    server = {"fyers": fyers, "upstox": upstox}[name]
    server.inclusive_to = inclusive
    with new_db(tmp_path, tf, "dhan.duckdb") as a, new_db(tmp_path, tf, "other.duckdb") as b:
        days = 28 if name == "upstox" else 89
        Downloader(a, make_client(dhan)).run(a.get_instruments(symbols=list(SYMS)), make_opts(tf, window_days=days if tf.is_intraday else 365))
        stats, _ = run_provider(b, name, server, tf)
        assert stats["status"] == "completed", stats
        rows_a, rows_b = candles(a, tf), candles(b, tf)
        assert rows_a and rows_a == rows_b                                 # same keys, same OHLCV, no hole, no extra day, no duplicate
        k = tf.key_col
        assert b.con.execute(f"SELECT count(*) FROM (SELECT 1 FROM {tf.table} GROUP BY security_id, {k} HAVING count(*) > 1)").fetchone()[0] == 0


def test_the_windows_asked_of_each_provider_respect_its_own_range_limits(tmp_path, fyers, upstox):
    with new_db(tmp_path, INTRADAY_15, "f.duckdb") as f, new_db(tmp_path, INTRADAY_15, "u.duckdb") as u:
        run_provider(f, "fyers", fyers, INTRADAY_15, symbols=("SIMALPHA",))
        run_provider(u, "upstox", upstox, INTRADAY_15, symbols=("SIMALPHA",))
    f_days = [(date.fromisoformat(r["query"]["range_to"]) - date.fromisoformat(r["query"]["range_from"])).days for r in fyers.requests("/data/history")]
    assert f_days and max(f_days) <= 100
    u_days = []
    for r in upstox.requests("/v3/historical-candle"):
        parts = r["path"].strip("/").split("/")
        u_days.append((date.fromisoformat(parts[5]) - date.fromisoformat(parts[6])).days)
    assert u_days and max(u_days) <= 31


def test_a_range_before_the_providers_first_date_is_shortened_and_never_recorded(tmp_path):
    settings = SimpleNamespace(provider="upstox", intraday_date_format="datetime", client_id=None)
    with new_db(tmp_path, INTRADAY_15) as db:
        plan = build_download_plan(db, settings=settings, symbols=["SIMALPHA"], start_date=date(2019, 1, 1), end_date=date(2022, 3, 1))
        assert plan.opts.history_start == datetime(2022, 1, 1) and plan.clamped_from == datetime(2019, 1, 1)
        assert any("only from 2022-01-01" in n for n in plan.notes)
        assert min(w.start for p in plan.per_instrument for w in p.windows) >= datetime(2022, 1, 1)
        with pytest.raises(PlanError, match="no 15min data"):
            build_download_plan(db, settings=settings, symbols=["SIMALPHA"], start_date=date(2019, 1, 1), end_date=date(2021, 6, 1))


def test_an_unsupported_window_length_is_refused_for_the_provider(tmp_path):
    settings = SimpleNamespace(provider="upstox", intraday_date_format="datetime", client_id=None)
    with new_db(tmp_path, INTRADAY_15) as db:
        with pytest.raises(PlanError, match="exceeds Upstox's 28-day cap"):
            build_download_plan(db, settings=settings, symbols=["SIMALPHA"], start_date=date(2024, 1, 1), end_date=date(2024, 3, 1), window_days=60)


def test_a_daily_timestamp_that_is_not_midnight_ist_is_read_as_that_ist_date(tmp_path, fyers):
    fyers.daily_epoch = "utc_midnight"                                     # 05:30 IST, as some providers stamp daily bars
    with new_db(tmp_path, DAILY) as db:
        stats, _ = run_provider(db, "fyers", fyers, DAILY, symbols=("SIMALPHA",))
        assert stats["status"] == "completed" and stats["complete"] >= 1
        first_ist = db.con.execute("SELECT min(candle_date), count(*) FROM candles_daily").fetchone()
        assert first_ist[1] > 100
        raw = db.con.execute("SELECT epoch_raw % 86400 FROM candles_daily LIMIT 1").fetchone()[0]
        assert raw == 0                                                    # the provider's own epoch is preserved for audit


# --------------------------------------------------------------------------------- errors and limits
def test_a_rejected_credential_aborts_the_run_and_records_nothing(tmp_path, fyers, upstox):
    for name, server in (("fyers", fyers), ("upstox", upstox)):
        with new_db(tmp_path, DAILY, f"{name}.duckdb") as db:
            adapter = get_provider(name)
            env = env_for(name, server)
            env["FYERS_ACCESS_TOKEN" if name == "fyers" else "UPSTOX_ANALYTICS_TOKEN"] = "wrong-token"
            client = adapter.make_client(env, settings=STUB, label="daily", limiter=FAST())
            stats = Downloader(db, client, provider=adapter).run(db.get_instruments(symbols=["SIMALPHA"]), make_opts(DAILY, window_days=365))
            assert stats["status"] == "aborted" and "authentication rejected" in stats["note"]
            assert db.con.execute("SELECT count(*) FROM download_windows").fetchone()[0] == 0


def test_fyers_reports_an_expired_token_from_a_200_error_body_too(tmp_path, fyers):
    fyers.auth_error_style = "body200"
    with new_db(tmp_path, DAILY) as db:
        adapter = get_provider("fyers")
        client = adapter.make_client({**env_for("fyers", fyers), "FYERS_ACCESS_TOKEN": "stale"}, settings=STUB, label="daily", limiter=FAST())
        with pytest.raises(AuthError):
            adapter.fetch(client, db.get_instruments(symbols=["SIMALPHA"])[0], DAILY, SimpleNamespace(start=datetime(2024, 1, 1), end=datetime(2024, 2, 1)), make_opts(DAILY))


@pytest.mark.parametrize("name", ["fyers", "upstox"])
def test_rate_limit_answers_are_retried_counted_and_never_lose_a_window(tmp_path, fyers, upstox, name):
    server = {"fyers": fyers, "upstox": upstox}[name]
    server.fail(times=2, status=429, headers={"Retry-After": "0"})
    ledger = UsageLedger(tmp_path / "ledger.sqlite")
    budget = RequestBudget(ledger, ledger_label(name, "daily"), max_per_run=1000, max_per_day=1000, provider=name)
    with new_db(tmp_path, DAILY) as db:
        stats, client = run_provider(db, name, server, DAILY, symbols=("SIMALPHA",), budget=budget)
        assert stats["status"] == "completed" and stats["failed"] == 0
        made = len(server.log)
        assert made >= 3 and client.requests_sent == made                  # the two 429s were counted like any attempt
        assert read_usage(tmp_path / "ledger.sqlite", provider=name) == {f"{name}/daily": made}
        assert read_usage(tmp_path / "ledger.sqlite", provider="dhan") == {}  # Dhan's own total is untouched


def test_persistent_server_errors_make_the_window_retriable_not_lost(tmp_path, fyers):
    fyers.fail(times=50, status=503)
    with new_db(tmp_path, DAILY) as db:
        stats, _ = run_provider(db, "fyers", fyers, DAILY, symbols=("SIMALPHA",))
        assert stats["retriable"] >= 1 and db.con.execute("SELECT count(*) FROM candles_daily").fetchone()[0] == 0
        fyers.rules.clear()
        again, _ = run_provider(db, "fyers", fyers, DAILY, symbols=("SIMALPHA",))
        assert again["status"] == "completed" and db.con.execute("SELECT count(*) FROM candles_daily").fetchone()[0] > 0


def test_a_malformed_provider_answer_fails_the_window_and_stores_nothing(tmp_path, fyers):
    fyers.fail(times=1, status=200, body={"s": "ok", "candles": [[1, 2, 3]]})          # rows too short
    with new_db(tmp_path, DAILY) as db:
        stats, _ = run_provider(db, "fyers", fyers, DAILY, symbols=("SIMALPHA",))
        assert stats["failed"] == 1
        code = db.con.execute("SELECT error_code FROM download_windows WHERE status = 'failed'").fetchone()[0]
        failed_from, failed_to = db.con.execute("SELECT window_from, window_to FROM download_windows WHERE status = 'failed'").fetchone()
        assert code == "malformed_response"
        assert db.con.execute("SELECT count(*) FROM candles_daily WHERE candle_date >= ? AND candle_date < ?",
                              [failed_from, failed_to]).fetchone()[0] == 0                # the failed window stored nothing


def test_each_provider_has_its_own_daily_cap_in_the_shared_ledger(tmp_path):
    ledger = UsageLedger(tmp_path / "l.sqlite")
    a = RequestBudget(ledger, "daily", max_per_run=100, max_per_day=3, provider="dhan")
    b = RequestBudget(ledger, "fyers/daily", max_per_run=100, max_per_day=3, provider="fyers")
    for _ in range(3):
        a.acquire()
    with pytest.raises(RequestLimitReached):
        a.acquire()
    for _ in range(3):                                                  # Dhan being at its cap does not block FYERS
        b.acquire()
    with pytest.raises(RequestLimitReached):
        b.acquire()
    assert provider_of_label("daily") == "dhan" and provider_of_label("fyers/15min") == "fyers"


# ----------------------------------------------------------------------------- provenance & conflicts
def test_every_stored_candle_and_window_names_its_provider_and_legacy_rows_stay_unlabelled(tmp_path, dhan, fyers):
    with new_db(tmp_path, DAILY) as db:
        stats, _ = run_provider(db, "fyers", fyers, DAILY, symbols=("SIMALPHA",))
        assert db.con.execute("SELECT DISTINCT provider FROM candles_daily").fetchall() == [("fyers",)]
        assert db.con.execute("SELECT DISTINCT provider FROM download_windows").fetchall() == [("fyers",)]
        Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=["SIMBETA"]), make_opts(DAILY, window_days=365))
        assert db.con.execute("SELECT DISTINCT provider FROM candles_daily WHERE security_id = 900002").fetchall() == [("dhan",)]


def test_an_old_database_gains_provenance_columns_without_any_row_changing(tmp_path, dhan):
    path = tmp_path / "old.duckdb"
    with CandleDB(DAILY, path) as db:
        load_universe(db, DETAILED)
        Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=["SIMALPHA"]), make_opts(DAILY, window_days=365))
        before = db.con.execute("SELECT * EXCLUDE (provider) FROM candles_daily ORDER BY ALL").fetchall()
        for table, col in (("candles_daily", "provider"), ("download_windows", "provider"), ("candle_revisions", "old_provider"),
                           ("candle_revisions", "new_provider"), ("refresh_changes", "old_provider"), ("refresh_changes", "new_provider")):
            db.con.execute(f"ALTER TABLE {table} DROP COLUMN {col}")             # what a pre-provider database looks like
        db.con.execute("UPDATE download_meta SET value = '1' WHERE key = 'schema_version'")
    with CandleDB(DAILY, path) as db:                                           # reopened by the new code: migrated in place
        assert db.get_meta("schema_version") == "2" and "provider NULL" in db.get_meta("provenance_note")
        assert db.con.execute("SELECT * EXCLUDE (provider) FROM candles_daily ORDER BY ALL").fetchall() == before
        assert db.con.execute("SELECT count(*) FROM candles_daily WHERE provider IS NOT NULL").fetchone()[0] == 0   # nothing relabelled
    with CandleDB(DAILY, path):                                                 # and idempotent
        pass


def test_switching_providers_never_replaces_existing_candles_it_records_the_disagreement(tmp_path, dhan, fyers):
    with new_db(tmp_path, DAILY) as db:
        Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=["SIMALPHA"]), make_opts(DAILY, window_days=365))
        stored = candles(db, DAILY)
        last_day = db.con.execute("SELECT max(candle_date) FROM candles_daily").fetchone()[0]
        # FYERS serves a different close for the last stored day (a conflicting candle) when asked for the overlap
        sims_f = fyers.instruments["NSE:SIMALPHA-EQ"]
        sims_f.base = sims_f.base * 1.5                                          # every FYERS value now differs from Dhan's
        run_provider(db, "fyers", fyers, DAILY, symbols=("SIMALPHA",), refetch=True)
        assert candles(db, DAILY) == stored                                      # the stored candles are untouched
        n = db.con.execute("SELECT count(*) FROM provider_disagreements").fetchone()[0]
        assert n > 0
        row = db.con.execute("SELECT stored_provider, other_provider, old_close, new_close FROM provider_disagreements LIMIT 1").fetchone()
        assert row[0] == "dhan" and row[1] == "fyers" and row[2] != row[3]
        assert db.con.execute("SELECT count(DISTINCT provider) FROM candles_daily").fetchone()[0] == 1   # still one source per candle


def test_changing_the_default_provider_neither_resets_checkpoints_nor_requests_covered_ranges_again(tmp_path, dhan, fyers):
    settings = SimpleNamespace(provider="fyers", intraday_date_format="datetime", client_id=None)
    with new_db(tmp_path, DAILY) as db:
        Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=list(SYMS)), make_opts(DAILY, window_days=365))
        windows_before = db.con.execute("SELECT count(*), sum(attempts) FROM download_windows").fetchone()
        plan = build_download_plan(db, settings=settings, symbols=list(SYMS), start_date=date(2023, 6, 1), end_date=date(2024, 6, 3), overlap_days=0)
        assert plan.requests == 0                                                 # everything is already covered, whoever supplied it
        assert db.con.execute("SELECT count(*), sum(attempts) FROM download_windows").fetchone() == windows_before


def test_a_range_one_provider_found_empty_can_be_filled_from_another_only_when_asked(tmp_path):
    settings = SimpleNamespace(provider="fyers", intraday_date_format="datetime", client_id=None)
    with new_db(tmp_path, DAILY) as db:
        inst = db.get_instruments(symbols=["SIMALPHA"])[0]
        empty = empty_result(DAILY)
        db.commit_window(inst, Window(datetime(2023, 1, 1), datetime(2023, 3, 1)), candles=empty.candles, anomalies=empty.anomalies,
                         zero_volume=0, attempts=1, http_status=200, provider="upstox")
        kw = dict(symbols=["SIMALPHA"], start_date=date(2023, 1, 1), end_date=date(2023, 3, 1), overlap_days=0, window_days=365)
        assert build_download_plan(db, settings=settings, **kw).requests == 0
        assert build_download_plan(db, settings=settings, foreign_empty_is_gap=True, **kw).requests == 1


def test_a_second_run_with_the_same_provider_adds_nothing(tmp_path, upstox):
    with new_db(tmp_path, INTRADAY_15) as db:
        run_provider(db, "upstox", upstox, INTRADAY_15, symbols=("SIMALPHA",))
        n1 = db.con.execute("SELECT count(*) FROM candles_15min").fetchone()[0]
        again, _ = run_provider(db, "upstox", upstox, INTRADAY_15, symbols=("SIMALPHA",), overlap_days=7)
        assert db.con.execute("SELECT count(*) FROM candles_15min").fetchone()[0] == n1
        assert db.con.execute("SELECT count(*) FROM candle_revisions").fetchone()[0] == 0


# ------------------------------------------------------------------------------ connection tests
def test_connection_tests_report_success_and_failure_without_leaking_the_token(tmp_path, fyers, upstox, dhan):
    for name, server in (("fyers", fyers), ("upstox", upstox)):
        adapter = get_provider(name)
        ok = adapter.test_connection(adapter.make_client(env_for(name, server), settings=STUB, label="test", limiter=FAST()))
        assert ok.ok and ok.state == "ok" and ok.requests_used == 1
        bad_env = dict(env_for(name, server))
        bad_env["FYERS_ACCESS_TOKEN" if name == "fyers" else "UPSTOX_ANALYTICS_TOKEN"] = "bad-token-xyz"
        bad = adapter.test_connection(adapter.make_client(bad_env, settings=STUB, label="test", limiter=FAST()))
        assert not bad.ok and bad.state == "auth_failed" and "bad-token-xyz" not in bad.message


def test_replacing_candles_stored_from_another_provider_needs_an_explicit_request(tmp_path, dhan, fyers):
    from dataclasses import replace
    from stockdata.refresh import Refresher, RefreshOptions
    with new_db(tmp_path, DAILY) as db:
        Downloader(db, make_client(dhan)).run(db.get_instruments(symbols=["SIMALPHA"]), make_opts(DAILY, window_days=365))
        before = candles(db, DAILY)
        fyers.instruments["NSE:SIMALPHA-EQ"].base *= 1.5                         # FYERS now serves different values
        adapter = get_provider("fyers")
        client = adapter.make_client(env_for("fyers", fyers), settings=STUB, label="daily", limiter=FAST())
        opts = RefreshOptions(start=datetime(2023, 6, 1), end=CUTOFF, window_days=365, provider="fyers")
        stats = Refresher(db, client, provider=adapter).run(db.get_instruments(symbols=["SIMALPHA"]), opts)
        assert stats["failed"] >= 1 and fyers.requests("/data/history") == []     # refused BEFORE any request: nothing spent
        assert db.con.execute("SELECT DISTINCT error_code FROM refresh_windows WHERE status = 'failed'").fetchall() == [("cross_provider_blocked",)]
        assert candles(db, DAILY) == before
        again = Refresher(db, client, provider=adapter).run(db.get_instruments(symbols=["SIMALPHA"]),
                                                            replace(opts, allow_cross_provider=True, retry_failed=True))
        assert again["status"] == "completed", again
        assert candles(db, DAILY) != before                                        # now the explicit repair replaced them
        assert db.con.execute("SELECT DISTINCT provider FROM candles_daily").fetchall() == [("fyers",)]
        olds = db.con.execute("SELECT DISTINCT old_provider, new_provider FROM refresh_changes WHERE change_type = 'revised'").fetchall()
        assert olds == [("dhan", "fyers")]                                         # both the audit trail and the revision say who replaced whom
        assert db.con.execute("SELECT DISTINCT old_provider, new_provider FROM candle_revisions").fetchall() == [("dhan", "fyers")]
