"""HTTP client shared by the FYERS and Upstox adapters: auth headers, multi-window rate limit, retry with backoff, and the
same guarantee the Dhan client gives - every outbound attempt (retries included) is counted through the shared request
budget BEFORE it is sent, so a crash can only over-count.

Provider specifics (what an error code means) live in the ``classify`` callback each adapter supplies; this class only
decides how to retry. Dhan keeps its own, unchanged client.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests

from stockdata.providers.base import MultiWindowLimiter
from stockdata.providers.errors import AuthError, PermanentError, ProviderError, ProviderSetupError, TransientError

log = logging.getLogger("stockdata.provider")

# classify(status, body, headers) must RAISE an AuthError / TransientError / PermanentError for anything that is not a
# usable answer, and return normally for an accepted response.
Classifier = Callable[[int, Any, Mapping[str, str]], None]


@dataclass
class ProviderResponse:
    data: Any
    http_status: int
    attempts: int


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value = headers.get("Retry-After")
    try:
        return min(max(float(value), 0.0), 120.0) if value is not None else None
    except (TypeError, ValueError):
        return None


def require_safe_url(base_url: str) -> None:
    host = urlparse(base_url)
    if host.scheme != "https" and host.hostname not in ("localhost", "127.0.0.1"):
        raise ProviderSetupError("Refusing to send a credential over a non-HTTPS base URL.")


class ProviderClient:
    def __init__(self, provider: str, base_url: str, *, headers: dict, limiter: MultiWindowLimiter, classify: Classifier,
                 budget=None, session: requests.Session | None = None, max_attempts: int = 5, backoff_base: float = 1.0,
                 backoff_cap: float = 60.0, timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None):
        require_safe_url(base_url)
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.limiter, self.budget, self._classify = limiter, budget, classify
        self.max_attempts, self.backoff_base, self.backoff_cap, self.timeout = max_attempts, backoff_base, backoff_cap, timeout
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.session = session or requests.Session()
        self._headers = dict(headers)
        self.requests_sent = 0

    def __repr__(self) -> str:                    # never expose the credential headers
        return f"ProviderClient({self.provider!r}, base_url={self.base_url!r})"

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1))
        return self._rng.uniform(ceiling / 2, ceiling)

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> ProviderResponse:
        url = path if path.startswith("http") else self.base_url + path
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.wait()
            try:
                data, status = self._once(url, params)
            except TransientError as exc:
                exc.attempts = attempt
                if exc.rate_limited:
                    self.limiter.penalize()
                if attempt == self.max_attempts:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else self._backoff(attempt)
                log.warning("%s: transient error (attempt %d/%d): %s; retrying in %.1fs", self.provider, attempt,
                            self.max_attempts, exc.message, delay)
                self._sleep(delay)
                continue
            except ProviderError as exc:
                exc.attempts = attempt
                raise
            self.limiter.relax()
            return ProviderResponse(data, status, attempt)
        raise AssertionError("unreachable")       # pragma: no cover

    def _once(self, url: str, params: Mapping[str, Any] | None) -> tuple[Any, int]:
        if self.budget is not None:
            self.budget.acquire()                 # counts this attempt or raises RequestLimitReached BEFORE sending
        self.requests_sent += 1
        try:
            resp = self.session.get(url, params=params, headers=self._headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise TransientError(f"network error: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        self._classify(resp.status_code, body, resp.headers)       # raises unless the answer is usable
        if body is None:
            raise TransientError(f"HTTP {resp.status_code} with a non-JSON/truncated body", http_status=resp.status_code)
        return body, resp.status_code


def http_failure(status: int, code: str | None, message: str, headers: Mapping[str, str], *,
                 auth_codes: frozenset[str] = frozenset(), transient_codes: frozenset[str] = frozenset(),
                 rate_codes: frozenset[str] = frozenset()) -> None:
    """The shared mapping from (status, provider code) to the error taxonomy. Raises; never returns for a failure."""
    detail = f"HTTP {status}" + (f" {code}" if code else "") + (f": {message}" if message else "")
    if status == 429 or (code and code in rate_codes):
        raise TransientError(detail, http_status=status, error_code=code, rate_limited=True, retry_after=retry_after_seconds(headers))
    if status in (401, 403) or (code and code in auth_codes):
        raise AuthError(detail, http_status=status, error_code=code)
    if status >= 500 or (code and code in transient_codes):
        raise TransientError(detail, http_status=status, error_code=code)
    if status >= 400:
        raise PermanentError(detail, http_status=status, error_code=code)
    if code:
        raise PermanentError(detail, http_status=status, error_code=code)
