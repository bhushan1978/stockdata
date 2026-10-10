"""The provider-adapter interface and the facts a provider reports about itself.

An adapter hides everything provider-specific (endpoint, authentication, request shape, error codes, instrument
identifiers, response layout) behind a few methods; everything after it - validation, the transactional window commit,
checkpoints, the request ledger, the job queue - is shared and unchanged, and every provider writes into the same
canonical candle tables.

Capability facts carry a *status*, because nothing about a provider may be assumed:

    sdk_confirmed          read from the provider's official client library (endpoint, parameters, resolutions, auth header)
    documented_unverified  the provider's published limit as remembered when this was written; NOT confirmed from here
    probe_verified         confirmed by a capability probe run against the real service from this installation
    unknown                nobody knows yet; the planner must not guess

The user interface shows the status next to every number, and probes upgrade ``documented_unverified`` to
``probe_verified`` only when a real response proves it. Free API access is subject to the provider's current terms,
permissions and limits; none of these numbers is a promise.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from stockdata.instruments import Instrument

SDK_CONFIRMED = "sdk_confirmed"
DOCUMENTED = "documented_unverified"
PROBE_VERIFIED = "probe_verified"
UNKNOWN = "unknown"
FACT_STATUSES = (SDK_CONFIRMED, DOCUMENTED, PROBE_VERIFIED, UNKNOWN)

TERMS_NOTE = ("Free API access is subject to the provider's current terms, permissions and limits. Check the provider's "
              "own pages before relying on any number shown here.")


# ------------------------------------------------------------------------------------------ capabilities
@dataclass(frozen=True)
class TimeframeSupport:
    supported: bool
    resolution: str                       # the provider's own label for this candle size
    max_window_days: int | None           # longest range one request may cover (None = no documented cap)
    earliest_date: date | None            # first date the provider is said to serve (None = unknown)
    status: str = UNKNOWN                 # status of max_window_days / earliest_date
    note: str = ""


@dataclass(frozen=True)
class RateLimit:
    per_second: float | None = None
    per_minute: int | None = None
    per_day: int | None = None
    status: str = UNKNOWN
    note: str = ""
    also: tuple[tuple[int, float], ...] = ()        # further (count, seconds) windows, e.g. 2000 per 30 minutes
    operating_day_cap: int | None = None            # the per-day cap this application uses unless told otherwise (<= per_day)

    def windows(self, fraction: float = 1.0) -> list[tuple[int, float]]:
        """(max requests, seconds) pairs for the limiter, optionally scaled for a safety margin."""
        out: list[tuple[int, float]] = []
        if self.per_second:
            rate = self.per_second * fraction
            out.append((int(rate), 1.0) if rate >= 1 else (1, 1.0 / rate))
        if self.per_minute:
            out.append((max(1, int(self.per_minute * fraction)), 60.0))
        out += [(max(1, int(c * fraction)), s) for c, s in self.also]
        return out

    def sustained_per_second(self, fraction: float = 1.0) -> float:
        """The slowest sustained rate any window allows: what a long download really runs at."""
        rates = [c / s for c, s in self.windows(fraction)]
        return min(rates) if rates else float("inf")


@dataclass(frozen=True)
class CredentialField:
    env_key: str
    label: str
    secret: bool = True
    required: bool = True
    placeholder: str = ""


@dataclass(frozen=True)
class AuthRequirement:
    fields: tuple[CredentialField, ...]
    token_kind: str                       # human description of the credential
    lifetime: str                         # how long a token is said to last
    interactive_login: bool               # does obtaining a fresh token need a person at a browser?
    unattended_note: str                  # what is known about unattended use (never an unverified promise)
    warn_before_seconds: int = 3 * 86400
    how_to_obtain: str = ""


@dataclass(frozen=True)
class Capabilities:
    provider: str
    label: str
    timeframes: Mapping[str, TimeframeSupport]
    rate_limit: RateLimit
    auth: AuthRequirement
    adjustment_basis: str                 # 'unknown' | 'raw' | 'split_bonus_adjusted' - how the provider's history is adjusted
    adjustment_status: str = UNKNOWN
    intraday_polling: bool = False        # may today's completed bars be fetched repeatedly (delayed, not streaming)
    instrument_mapping: str = ""          # how our instrument rows map to this provider's identifiers
    sources: tuple[str, ...] = ()         # where the facts above came from
    terms_note: str = TERMS_NOTE
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        def tf(t: TimeframeSupport) -> dict:
            return {"supported": t.supported, "resolution": t.resolution, "max_window_days": t.max_window_days,
                    "earliest_date": t.earliest_date.isoformat() if t.earliest_date else None, "status": t.status, "note": t.note}
        return {"provider": self.provider, "label": self.label,
                "timeframes": {k: tf(v) for k, v in self.timeframes.items()},
                "rate_limit": {"per_second": self.rate_limit.per_second, "per_minute": self.rate_limit.per_minute,
                               "per_day": self.rate_limit.per_day, "status": self.rate_limit.status, "note": self.rate_limit.note,
                               "also": [{"count": c, "seconds": s} for c, s in self.rate_limit.also],
                               "operating_day_cap": self.rate_limit.operating_day_cap,
                               "sustained_per_second": round(self.rate_limit.sustained_per_second(0.6), 3)},
                "auth": {"fields": [{"env_key": f.env_key, "label": f.label, "secret": f.secret, "required": f.required,
                                     "placeholder": f.placeholder} for f in self.auth.fields],
                         "token_kind": self.auth.token_kind, "lifetime": self.auth.lifetime,
                         "interactive_login": self.auth.interactive_login, "unattended_note": self.auth.unattended_note,
                         "how_to_obtain": self.auth.how_to_obtain},
                "adjustment_basis": self.adjustment_basis, "adjustment_status": self.adjustment_status,
                "intraday_polling": self.intraday_polling, "instrument_mapping": self.instrument_mapping,
                "sources": list(self.sources), "terms_note": self.terms_note, **dict(self.extra)}


@dataclass
class AuthStatus:
    """What can be said about a credential WITHOUT contacting the provider."""
    provider: str
    state: str                            # not_configured | expired | expiring_soon | ready
    configured: bool
    message: str
    expires_at_utc: str | None = None
    expires_at_ist: str | None = None
    seconds_left: int | None = None
    expiry_known: bool = False
    missing: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"provider": self.provider, "state": self.state, "configured": self.configured, "message": self.message,
                "expires_at_utc": self.expires_at_utc, "expires_at_ist": self.expires_at_ist, "seconds_left": self.seconds_left,
                "expiry_known": self.expiry_known, "missing": list(self.missing)}


@dataclass
class ConnectionResult:
    ok: bool
    state: str                            # ok | auth_failed | not_configured | network | rejected | rate_limited
    message: str
    requests_used: int = 0
    details: dict = field(default_factory=dict)


@dataclass
class RawResponse:
    body: Any
    http_status: int
    attempts: int


# ----------------------------------------------------------------------------------------- rate limiting
class MultiWindowLimiter:
    """Allows at most ``count`` requests in any ``seconds``-long window, for every configured window at once (for example
    10 per second AND 200 per minute). ``penalize`` stretches every window after a rate-limit answer."""

    def __init__(self, windows: Sequence[tuple[int, float]], *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if not windows or any(c < 1 or s <= 0 for c, s in windows):
            raise ValueError("limiter windows must be positive (count >= 1, seconds > 0)")
        self.windows = list(windows)
        self._clock, self._sleep = clock, sleep
        self._stamps = [deque() for _ in self.windows]
        self.penalty = 1.0

    def wait(self) -> None:
        while True:
            now = self._clock()
            need = 0.0
            for (count, seconds), stamps in zip(self.windows, self._stamps):
                span = seconds * self.penalty
                while stamps and stamps[0] <= now - span:
                    stamps.popleft()
                if len(stamps) >= count:
                    need = max(need, stamps[0] + span - now)
            if need <= 0:
                for stamps in self._stamps:
                    stamps.append(now)
                return
            self._sleep(need)

    def penalize(self) -> None:
        self.penalty = min(self.penalty * 1.5, 5.0)

    def relax(self) -> None:
        self.penalty = max(1.0, self.penalty * 0.9)


# --------------------------------------------------------------------------------------------- adapter
class ProviderAdapter(ABC):
    name: str = ""
    label: str = ""

    # --- facts
    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    def window_days(self, tf) -> int:
        """Window length this provider's requests use for ``tf`` (never above its cap)."""
        sup = self.capabilities().timeframes[tf.name]
        base = tf.default_window_days
        return min(base, sup.max_window_days) if sup.max_window_days else base

    def earliest(self, tf) -> date | None:
        return self.capabilities().timeframes[tf.name].earliest_date

    def supports(self, tf) -> bool:
        sup = self.capabilities().timeframes.get(tf.name)
        return bool(sup and sup.supported)

    # --- credentials
    def credential_status(self, env: Mapping[str, str], now: datetime | None = None) -> AuthStatus:
        from stockdata.providers.tokens import jwt_expiry     # local import: tokens.py has no provider dependencies
        caps = self.capabilities()
        missing = self.missing_credentials(env)
        if missing:
            return AuthStatus(self.name, "not_configured", False, f"{self.label}: not configured ({', '.join(missing)} missing)",
                              missing=missing)
        now = now or datetime.now(timezone.utc)
        exp = jwt_expiry(env.get(self.token_key_for(env)))
        if exp is None:
            return AuthStatus(self.name, "ready", True, f"{self.label}: credentials present; expiry cannot be read from the token "
                              "(run a connection test to verify them)")
        left = int((exp - now).total_seconds())
        st = AuthStatus(self.name, "ready", True, "", expires_at_utc=exp.isoformat(timespec="seconds"),
                        expires_at_ist=exp.astimezone(_IST).strftime("%Y-%m-%d %H:%M:%S"), seconds_left=left, expiry_known=True)
        if left <= 0:
            st.state, st.message = "expired", f"{self.label}: the token expired at {st.expires_at_ist} IST"
        elif left <= caps.auth.warn_before_seconds:
            st.state, st.message = "expiring_soon", f"{self.label}: the token expires at {st.expires_at_ist} IST"
        else:
            st.message = f"{self.label}: token valid until {st.expires_at_ist} IST"
        return st

    @abstractmethod
    def token_env_key(self) -> str:
        """Environment key of the credential whose expiry is tracked."""

    def token_key_for(self, env: Mapping[str, str]) -> str:
        """The credential actually in use (a provider that accepts alternative tokens overrides this)."""
        return self.token_env_key()

    def missing_credentials(self, env: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(f.env_key for f in self.capabilities().auth.fields if f.required and not env.get(f.env_key))

    # --- instruments
    @abstractmethod
    def map_instrument(self, inst: Instrument) -> str | None:
        """The provider's identifier for ``inst`` (symbol or instrument key), or None when it cannot be derived safely."""

    # --- requests
    @abstractmethod
    def make_client(self, env: Mapping[str, str], *, limits, label: str, budget=None, session=None, base_url: str | None = None):
        """A client that counts every attempt through ``budget`` (shared ledger) and honours the rate limits."""

    @abstractmethod
    def fetch(self, client, inst: Instrument, tf, window, opts) -> RawResponse:
        """One request for ``window`` (half-open [start, end)). Raises AuthError / TransientError / PermanentError."""

    @abstractmethod
    def to_canonical(self, tf, raw: RawResponse, window):
        """Provider response -> ``stockdata.validation.Validated`` (canonical candle frame). Raises ValidationError."""

    def empty_code(self, raw: RawResponse) -> str | None:
        """For an accepted but empty response, a short provider code to store on the window (audit only)."""
        return None

    @abstractmethod
    def test_connection(self, client) -> ConnectionResult:
        """One explicit, cheap, counted request that proves the credential works (no candles)."""


from zoneinfo import ZoneInfo as _Z        # noqa: E402  (kept at the bottom: only used by credential_status)
_IST = _Z("Asia/Kolkata")


# ------------------------------------------------------------------------------- normalisation helpers
CANONICAL_KEYS = ("open", "high", "low", "close", "volume", "timestamp")


def canonical_body(rows: Sequence[Sequence[Any]], *, epoch_index: int = 0) -> dict:
    """Candle rows [t, o, h, l, c, v, ...] with epoch-second timestamps -> the array layout ``validate_response`` consumes."""
    return {"timestamp": [r[epoch_index] for r in rows], "open": [r[1] for r in rows], "high": [r[2] for r in rows],
            "low": [r[3] for r in rows], "close": [r[4] for r in rows], "volume": [r[5] for r in rows]}


def sort_oldest_first(rows: list[list[Any]]) -> list[list[Any]]:
    return sorted(rows, key=lambda r: r[0])


def drop_extra_day(rows: list[list[Any]], window_end: datetime, ist_of: Callable[[Any], pd.Timestamp]) -> list[list[Any]]:
    """Some providers treat the 'to' date as inclusive, others as exclusive. Requests therefore ask for ``to = window end``
    (so the last day of the window is covered under BOTH readings) and the single extra day this may return is dropped
    here. Anything further outside the window is deliberately left in, so validation rejects it."""
    cut = pd.Timestamp(window_end)
    nxt = cut + pd.Timedelta(days=1)
    out = []
    for r in rows:
        t = ist_of(r[0])
        if cut <= t < nxt:
            continue
        out.append(r)
    return out
