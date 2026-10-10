"""Request safety limits: per-run cap, shared per-IST-day ledger, counting of retries. Simulated traffic only."""
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import make_client
from stockdata.budget import (COUNTER_CAVEAT, DEFAULT_MAX_PER_DAY, DEFAULT_MAX_PER_RUN, RequestBudget, RequestLimitReached,
                              UsageLedger, read_usage, validate_limits)
from stockdata.config import Settings
from stockdata.dhan import DhanClient, DhanTransientError, RateLimiter
from stockdata.instruments import Instrument
from stockdata.timeframes import INTRADAY_15

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def ledger_at(tmp_path, dt=datetime(2026, 10, 10, 11, 0, tzinfo=IST)):
    clock = Clock(dt)
    return UsageLedger(tmp_path / "ledger.sqlite", clock=clock), clock


def test_defaults_are_the_requested_limits():
    assert (DEFAULT_MAX_PER_RUN, DEFAULT_MAX_PER_DAY) == (60_000, 80_000)
    s = Settings.from_env("x")
    assert (s.max_requests_per_run, s.max_requests_per_day) == (60_000, 80_000)


def test_limits_are_configurable_and_sanity_checked(monkeypatch):
    monkeypatch.setenv("STOCKDATA_MAX_REQUESTS_PER_RUN", "10")
    monkeypatch.setenv("STOCKDATA_MAX_REQUESTS_PER_DAY", "20")
    s = Settings.from_env("x")
    assert (s.max_requests_per_run, s.max_requests_per_day) == (10, 20)
    for bad in ((0, 10), (10, 0), (10, 100_001)):          # above Dhan's documented 100,000/day is refused
        with pytest.raises(ValueError):
            validate_limits(*bad)
    validate_limits(60_000, 100_000)


def test_run_cap_stops_before_the_next_attempt_and_refused_attempts_are_not_counted(tmp_path):
    ledger, _ = ledger_at(tmp_path)
    b = RequestBudget(ledger, "daily", max_per_run=3, max_per_day=100)
    for _ in range(3):
        b.acquire()
    with pytest.raises(RequestLimitReached) as e:
        b.acquire()
    assert e.value.kind == "run" and e.value.limit == 3
    assert ledger.usage() == {"daily": 3} and b.run_used == 3


def test_day_cap_is_shared_by_both_timeframes_and_by_separate_budget_objects(tmp_path):
    ledger, _ = ledger_at(tmp_path)
    daily = RequestBudget(ledger, "daily", max_per_run=100, max_per_day=5)
    intraday = RequestBudget(UsageLedger(ledger.path, clock=ledger._clock), "15min", max_per_run=100, max_per_day=5)  # "other process"
    for _ in range(3):
        daily.acquire()
    for _ in range(2):
        intraday.acquire()
    with pytest.raises(RequestLimitReached) as e:
        daily.acquire()
    assert e.value.kind == "day" and e.value.used == 5
    with pytest.raises(RequestLimitReached):
        intraday.acquire()
    assert ledger.usage() == {"15min": 2, "daily": 3}


def test_usage_persists_across_ledger_instances(tmp_path):
    ledger, clock = ledger_at(tmp_path)
    RequestBudget(ledger, "daily", max_per_run=10, max_per_day=10).acquire()
    ledger.close()
    again = UsageLedger(tmp_path / "ledger.sqlite", clock=clock)
    assert again.usage() == {"daily": 1}


def test_the_day_is_the_ist_calendar_day_not_utc(tmp_path):
    ledger, clock = ledger_at(tmp_path, datetime(2026, 10, 10, 23, 59, tzinfo=IST))
    b = RequestBudget(ledger, "daily", max_per_run=100, max_per_day=2)
    b.acquire()
    b.acquire()
    with pytest.raises(RequestLimitReached):
        b.acquire()
    clock.dt = datetime(2026, 10, 11, 0, 1, tzinfo=IST)               # IST midnight passed: a new day, count resets
    b.acquire()
    assert ledger.usage("2026-10-10") == {"daily": 2} and ledger.usage("2026-10-11") == {"daily": 1}
    # 19:00 UTC on 10 Oct is already 00:30 IST on 11 Oct
    clock.dt = datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)
    assert ledger.day() == "2026-10-11"


def test_a_limit_hit_is_logged_in_the_ledger(tmp_path):
    ledger, _ = ledger_at(tmp_path)
    b = RequestBudget(ledger, "daily", max_per_run=100, max_per_day=1)
    b.acquire()
    with pytest.raises(RequestLimitReached):
        b.acquire()
    rows = ledger._con.execute("SELECT kind, used, limit_value FROM limit_events").fetchall()
    assert rows == [("day", 1, 1)]


def test_concurrent_processes_never_exceed_the_day_cap(tmp_path):
    """4 real processes x 40 attempts against a cap of 100: exactly 100 succeed, 60 are refused."""
    script = textwrap.dedent("""
        import sys
        from stockdata.budget import RequestBudget, RequestLimitReached, UsageLedger
        ok = refused = 0
        b = RequestBudget(UsageLedger(sys.argv[1]), sys.argv[2], max_per_run=1000, max_per_day=100)
        for _ in range(40):
            try:
                b.acquire(); ok += 1
            except RequestLimitReached:
                refused += 1
        print(ok, refused)
    """)
    path = str(tmp_path / "shared.sqlite")
    procs = [subprocess.Popen([sys.executable, "-c", script, path, label], cwd=ROOT, stdout=subprocess.PIPE, text=True)
             for label in ("daily", "15min", "daily", "15min")]
    results = [tuple(map(int, p.communicate(timeout=120)[0].split())) for p in procs]
    assert sum(r[0] for r in results) == 100 and sum(r[1] for r in results) == 60
    assert sum(read_usage(path).values()) == 100


def test_reading_usage_never_creates_or_locks_a_ledger(tmp_path):
    missing = tmp_path / "nope.sqlite"
    assert read_usage(missing) == {} and read_usage(missing, history=7) == [] and not missing.exists()


def test_summary_states_the_account_wide_caveat(tmp_path):
    ledger, _ = ledger_at(tmp_path)
    b = RequestBudget(ledger, "daily", max_per_run=10, max_per_day=20)
    b.acquire()
    text = b.summary()
    assert "1 of 10" in text and "of 20" in text and COUNTER_CAVEAT in text
    assert "other applications" in text.lower() and "100,000" in text


# ---------------------------------------------------------------- the client counts every attempt
def client_with(fake, ledger, label="daily", **kw):
    budget = RequestBudget(ledger, label, max_per_run=kw.pop("per_run", 100), max_per_day=kw.pop("per_day", 100))
    c = DhanClient("test-token", fake.base_url, limiter=RateLimiter(10_000, sleep=lambda s: None),
                   max_attempts=kw.pop("max_attempts", 5), sleep=lambda s: None, budget=budget)
    return c, budget


PAYLOAD = INTRADAY_15.payload(Instrument("NSE_EQ", 900001, "X", "EQUITY"), datetime(2024, 5, 6), datetime(2024, 5, 8),
                              intraday_date_format="datetime", client_id=None)


def test_retries_are_counted_as_attempts(fake, tmp_path):
    ledger, _ = ledger_at(tmp_path)
    fake.fail(times=2, status=500)
    client, budget = client_with(fake, ledger)
    assert client.post(INTRADAY_15.endpoint, PAYLOAD).attempts == 3
    assert budget.run_used == 3 == client.requests_sent == len(fake.log) == sum(ledger.usage().values())


def test_network_failures_and_auth_failures_are_counted_too(fake, tmp_path):
    ledger, _ = ledger_at(tmp_path)
    fake.fail(times=1, status="drop")
    client, budget = client_with(fake, ledger)
    client.post(INTRADAY_15.endpoint, PAYLOAD)                         # 1 dropped + 1 ok
    bad = DhanClient("wrong", fake.base_url, limiter=RateLimiter(10_000), budget=RequestBudget(ledger, "daily", max_per_run=9, max_per_day=100))
    with pytest.raises(Exception):
        bad.post(INTRADAY_15.endpoint, PAYLOAD)
    assert sum(ledger.usage().values()) == 3


def test_the_cap_applies_between_retries_and_nothing_further_is_sent(fake, tmp_path):
    ledger, _ = ledger_at(tmp_path)
    fake.fail(times=100, status=503)
    client, budget = client_with(fake, ledger, per_run=2)
    with pytest.raises(RequestLimitReached) as e:
        client.post(INTRADAY_15.endpoint, PAYLOAD)
    assert e.value.kind == "run" and len(fake.log) == 2               # the 3rd attempt was refused BEFORE being sent
    assert not isinstance(e.value, DhanTransientError)


def test_the_rate_limiter_is_still_in_force_with_a_budget(fake, tmp_path):
    ledger, _ = ledger_at(tmp_path)
    waits = []
    now = [0.0]
    lim = RateLimiter(4, clock=lambda: now[0], sleep=lambda s: (waits.append(s), now.__setitem__(0, now[0] + s)))
    client = DhanClient("test-token", fake.base_url, limiter=lim, budget=RequestBudget(ledger, "daily", max_per_run=9, max_per_day=9))
    for _ in range(3):
        client.post(INTRADAY_15.endpoint, PAYLOAD)
    assert waits == pytest.approx([0.25, 0.25])
