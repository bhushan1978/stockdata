"""HTTP client for the Dhan data API: auth headers, rate limit, retry/backoff.

Error taxonomy (decides how a window is recorded):
  DhanAuthError       token rejected  -> abort the whole run, record nothing
  DhanTransientError  network/429/5xx -> retried here; if exhausted the window is 'retriable'
  DhanPermanentError  request rejected -> window is 'failed' (never retried automatically)

The Dhan error-code sets below come from the published error list as recalled
and are UNVERIFIED from this build environment. Unknown codes are treated as
permanent for the window and are *never* interpreted as "no data".
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import requests

from stockdata.providers.errors import AuthError, PermanentError, ProviderError, TransientError

if TYPE_CHECKING:  # pragma: no cover
    from stockdata.budget import RequestBudget

log = logging.getLogger("stockdata.dhan")

AUTH_CODES = {"DH-901", "DH-902", "DH-903"}
# Dhan's data endpoints answer a rejected access token as HTTP 400 DH-906 "Invalid Token" (seen on a real account), although the
# published table lists DH-906 as an order error. Only a DH-906 whose message names the token is read as an authentication
# failure; any other DH-906 stays a permanent request error.
TOKEN_REJECTION_CODES = {"DH-906"}
RATE_LIMIT_CODES = {"DH-904", "DH-805"}
TRANSIENT_CODES = {"DH-908", "DH-909"}


class DhanError(ProviderError):
    """Dhan's errors are the shared provider errors with a Dhan name (the downloader catches the shared base classes)."""


class DhanAuthError(DhanError, AuthError):
    pass


class DhanPermanentError(DhanError, PermanentError):
    pass


class DhanTransientError(DhanError, TransientError):
    pass


@dataclass
class DhanResponse:
    data: dict
    http_status: int
    attempts: int


class RateLimiter:
    """Evenly spaced requests (``rps`` per second), slowed temporarily after a 429."""

    def __init__(self, rps: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if rps <= 0:
            raise ValueError("rps must be positive")
        self.interval = 1.0 / rps
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self.penalty = 1.0

    def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval * self.penalty

    def penalize(self) -> None:
        self.penalty = min(self.penalty * 1.5, 5.0)

    def relax(self) -> None:
        self.penalty = max(1.0, self.penalty * 0.9)


class DhanClient:
    def __init__(self, access_token: str, base_url: str, *, client_id: str | None = None,
                 limiter: RateLimiter, max_attempts: int = 5, backoff_base: float = 1.0,
                 backoff_cap: float = 60.0, timeout: float = 30.0,
                 session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None,
                 budget: "RequestBudget | None" = None):
        self.base_url = base_url.rstrip("/")
        self.budget = budget
        self.limiter = limiter
        self.max_attempts = max_attempts
        self.backoff_base, self.backoff_cap, self.timeout = backoff_base, backoff_cap, timeout
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.session = session or requests.Session()
        self._headers = {"access-token": access_token, "Content-Type": "application/json",
                         "Accept": "application/json"}
        if client_id:
            self._headers["client-id"] = client_id
        self.requests_sent = 0

    def __repr__(self) -> str:  # never expose the token
        return f"DhanClient(base_url={self.base_url!r})"

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1))
        return self._rng.uniform(ceiling / 2, ceiling)

    def post(self, path: str, payload: dict) -> DhanResponse:
        return self._request("POST", path, payload)

    def get_json(self, path: str) -> dict:
        """GET (the profile call of the connection test). Counted in the same budget and classified like any other call."""
        return self._request("GET", path, None).data

    def _request(self, method: str, path: str, payload: dict | None) -> DhanResponse:
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.wait()
            try:
                data, status = self._once(path, payload, method)
            except DhanTransientError as exc:
                exc.attempts = attempt
                if exc.rate_limited:
                    self.limiter.penalize()
                if attempt == self.max_attempts:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else self._backoff(attempt)
                log.warning("transient error on %s (attempt %d/%d): %s; retrying in %.1fs",
                            path, attempt, self.max_attempts, exc.message, delay)
                self._sleep(delay)
                continue
            except DhanError as exc:
                exc.attempts = attempt
                raise
            self.limiter.relax()
            return DhanResponse(data, status, attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _once(self, path: str, payload: dict | None, method: str = "POST") -> tuple[dict, int]:
        if self.budget is not None:
            self.budget.acquire()     # counts this attempt (retries included) or raises RequestLimitReached BEFORE sending
        self.requests_sent += 1
        try:
            if method == "GET":
                resp = self.session.get(self.base_url + path, headers=self._headers, timeout=self.timeout)
            else:
                resp = self.session.post(self.base_url + path, json=payload,
                                         headers=self._headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DhanTransientError(f"network error: {type(exc).__name__}") from exc
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = None
        code = body.get("errorCode") if isinstance(body, dict) else None
        msg = (body.get("errorMessage") or body.get("errorType") or "") if isinstance(body, dict) else ""
        detail = f"HTTP {status}" + (f" {code}" if code else "") + (f": {msg}" if msg else "")
        if status == 429 or code in RATE_LIMIT_CODES:
            raise DhanTransientError(detail, http_status=status, error_code=code, rate_limited=True,
                                     retry_after=_retry_after(resp))
        if status in (401, 403) or code in AUTH_CODES or (code in TOKEN_REJECTION_CODES and "token" in msg.lower()):
            raise DhanAuthError(detail, http_status=status, error_code=code)
        if status >= 500 or code in TRANSIENT_CODES:
            raise DhanTransientError(detail, http_status=status, error_code=code)
        if status >= 400 or code:
            raise DhanPermanentError(detail, http_status=status, error_code=code)
        if not isinstance(body, dict):
            raise DhanTransientError(f"HTTP {status} with a non-JSON/truncated body", http_status=status)
        return body, status


def _retry_after(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return min(max(float(value), 0.0), 120.0) if value is not None else None
    except ValueError:
        return None
