"""Upstox API adapter (historical candles, API v3).

Confirmed from Upstox's official Python SDK (``upstox-python-sdk`` 2.30.0) and therefore tagged ``sdk_confirmed``:
  * host ``https://api.upstox.com``;
  * ``GET /v3/historical-candle/{instrumentKey}/{unit}/{interval}/{to_date}/{from_date}``
    (and ``/v3/historical-candle/intraday/{instrumentKey}/{unit}/{interval}`` for the current session);
  * authentication ``Authorization: Bearer <token>``.

Documented by Upstox but NOT confirmable from here (``documented_unverified``): the longest range per request for each
unit (minute intervals up to 15: one month; longer minute/hour intervals: one quarter; days: ten years), the earliest
data (minutes/hours from January 2022, days from January 2000), the request quotas, and whether candles are adjusted.
The capability probe measures them.

Credential: the read-only **Analytics Token** is preferred. Upstox documents it as valid for one year, usable with GET
(read) endpoints only, and one token per account; it needs no daily browser login, so it is the credential that makes
unattended updates plausible. That is still unverified until a scheduled run has succeeded with it.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping
from urllib.parse import quote

import pandas as pd

from stockdata.instruments import Instrument
from stockdata.providers import base
from stockdata.providers.base import (AuthRequirement, Capabilities, ConnectionResult, CredentialField, DOCUMENTED, RateLimit,
                                      RawResponse, TimeframeSupport, UNKNOWN, canonical_body, drop_extra_day, sort_oldest_first)
from stockdata.providers.errors import AuthError, PermanentError, ProviderSetupError
from stockdata.providers.http import ProviderClient, http_failure, require_safe_url
from stockdata.timeutil import IST
from stockdata.validation import ValidationError, Validated, validate_response

HOST = "https://api.upstox.com"
ANALYTICS_KEY, ACCESS_KEY = "UPSTOX_ANALYTICS_TOKEN", "UPSTOX_ACCESS_TOKEN"
BASE_URL_KEY = "UPSTOX_BASE_URL"                     # tests point this at a local fake server
UNIT = {"daily": ("days", 1), "15min": ("minutes", 15)}
AUTH_CODES = frozenset({"UDAPI100050", "UDAPI100051"})
RATE_CODES = frozenset({"UDAPI10005"})
PROBE_KEY = "NSE_EQ|INE002A01018"                    # a fixed, very liquid instrument used only by the connection test


def token_of(env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """(token, which) - the Analytics Token when present, otherwise a normal access token."""
    if env.get(ANALYTICS_KEY):
        return env[ANALYTICS_KEY], ANALYTICS_KEY
    if env.get(ACCESS_KEY):
        return env[ACCESS_KEY], ACCESS_KEY
    return None, None


class UpstoxAdapter(base.ProviderAdapter):
    name = "upstox"
    label = "Upstox"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            provider=self.name, label=self.label,
            timeframes={
                "daily": TimeframeSupport(True, "days / 1", 3000, date(2000, 1, 1), DOCUMENTED,
                                          "documented: up to ten years per request (kept below that) and data from January 2000; unverified"),
                "15min": TimeframeSupport(True, "minutes / 15", 28, date(2022, 1, 1), DOCUMENTED,
                                          "documented: up to one month per request for intervals up to 15 minutes and data from "
                                          "January 2022; unverified. Fewer than five years of 15-minute history are available."),
            },
            rate_limit=RateLimit(per_second=50, per_minute=500, also=((2000, 1800.0),), operating_day_cap=40_000, status=DOCUMENTED,
                                 note="published quotas as remembered (per second, per minute, per 30 minutes); the 30-minute "
                                      "window is the one that limits a long download; unverified"),
            auth=AuthRequirement(
                fields=(CredentialField(ANALYTICS_KEY, "Analytics Token (read-only, preferred)", secret=True, required=False),
                        CredentialField(ACCESS_KEY, "Access token (OAuth, only if no Analytics Token)", secret=True, required=False)),
                token_kind="Upstox Analytics Token (read-only) or an OAuth access token",
                lifetime="Analytics Token: one year from generation (documented); OAuth access token: expires daily",
                interactive_login=False,
                unattended_note=("The Analytics Token needs no daily login, so unattended updates are plausible; this is NOT "
                                 "claimed as verified until a scheduled run has completed successfully with it."),
                warn_before_seconds=14 * 86400,
                how_to_obtain="In the Upstox developer console open your app's Analytics tab and generate the Analytics Token "
                              "(read-only, no redirect). Only one such token exists per account at a time."),
            adjustment_basis="unknown", adjustment_status=UNKNOWN,
            intraday_polling=True,
            instrument_mapping="NSE_EQ|<ISIN> built from the instrument master's ISIN; instruments without an ISIN are reported "
                               "as unmapped, never guessed",
            sources=("upstox-python-sdk 2.30.0 (official SDK): endpoint paths and authentication header",
                     "published ranges and quotas as remembered when written: unverified"))

    def token_env_key(self) -> str:
        return ANALYTICS_KEY

    def token_key_for(self, env):                                # either token satisfies the requirement
        return token_of(env)[1] or ANALYTICS_KEY

    def missing_credentials(self, env):
        return () if token_of(env)[0] else (ANALYTICS_KEY,)

    def instrument_key(self, inst: Instrument) -> str | None:
        return self.map_instrument(inst)

    def map_instrument(self, inst: Instrument) -> str | None:
        if inst.exchange_segment != "NSE_EQ" or not inst.isin or len(inst.isin) != 12:
            return None
        return f"NSE_EQ|{inst.isin.upper()}"

    # ---------------------------------------------------------------------------------------- client
    def make_client(self, env: Mapping[str, str], *, settings, label: str, budget=None, session=None,
                    max_attempts: int = 5, limiter=None) -> ProviderClient:
        token, _ = token_of(env)
        if not token:
            raise ProviderSetupError("Upstox credentials are not set: UPSTOX_ANALYTICS_TOKEN (preferred) or UPSTOX_ACCESS_TOKEN "
                                     "is required (Providers screen, or the untracked .env file).")
        base_url = (env.get(BASE_URL_KEY) or HOST).rstrip("/")
        require_safe_url(base_url)
        rl = self.capabilities().rate_limit
        override = getattr(settings, "rps_override", None)
        lim = limiter or base.MultiWindowLimiter(rl.windows(fraction=(override / rl.per_second) if override else 0.6))
        return ProviderClient(self.name, base_url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                              limiter=lim, classify=classify, budget=budget, session=session, max_attempts=max_attempts)

    def fetch(self, client: ProviderClient, inst: Instrument, tf, window, opts) -> RawResponse:
        key = self.map_instrument(inst)
        if key is None:
            raise PermanentError(f"{inst.symbol}: no Upstox instrument key can be derived (an NSE equity ISIN is required)",
                                 error_code="unmapped_instrument")
        unit, interval = UNIT[tf.name]
        # to_date = the window's end date: covers the last day whether the provider reads it as inclusive or exclusive
        path = (f"/v3/historical-candle/{quote(key, safe='')}/{unit}/{interval}/"
                f"{window.end.strftime('%Y-%m-%d')}/{window.start.strftime('%Y-%m-%d')}")
        resp = client.get(path)
        return RawResponse(resp.data, resp.http_status, resp.attempts)

    # --------------------------------------------------------------------------------- normalisation
    def to_canonical(self, tf, raw: RawResponse, window) -> Validated:
        body = raw.body
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(data, dict) or \
                not isinstance(data.get("candles"), list):
            raise ValidationError("malformed_response", "unexpected Upstox response shape")
        rows = []
        for r in data["candles"]:
            if not isinstance(r, list) or len(r) < 6:
                raise ValidationError("malformed_response", "Upstox candles are not [time, open, high, low, close, volume, oi] rows")
            try:
                epoch = int(pd.Timestamp(r[0]).timestamp())
            except (ValueError, TypeError) as exc:
                raise ValidationError("timestamp_conversion", f"candle time {r[0]!r} is not an ISO-8601 timestamp") from exc
            rows.append([epoch, *r[1:6]])
        rows = drop_extra_day(sort_oldest_first(rows), window.end, _ist)
        return validate_response(tf, canonical_body(rows), window)

    # ------------------------------------------------------------------------------ connection test
    def test_connection(self, client: ProviderClient) -> ConnectionResult:
        today = datetime.now(IST).date()
        path = (f"/v3/historical-candle/{quote(PROBE_KEY, safe='')}/days/1/{today:%Y-%m-%d}/"
                f"{(pd.Timestamp(today) - pd.Timedelta(days=7)):%Y-%m-%d}")
        try:
            resp = client.get(path)
        except AuthError as exc:
            return ConnectionResult(False, "auth_failed", f"Upstox rejected the token ({exc.message}); generate a new token",
                                    requests_used=client.requests_sent)
        except PermanentError as exc:
            return ConnectionResult(False, "rejected", f"Upstox refused the request: {exc.message}", requests_used=client.requests_sent)
        except Exception as exc:                      # noqa: BLE001
            return ConnectionResult(False, "network", f"could not complete the test: {type(exc).__name__}: {exc}", requests_used=client.requests_sent)
        ok = isinstance(resp.data, dict) and resp.data.get("status") == "success"
        return ConnectionResult(ok, "ok" if ok else "rejected",
                                "Upstox accepted the token for historical candles" if ok else "unexpected answer",
                                requests_used=client.requests_sent)


def _ist(epoch) -> pd.Timestamp:
    return pd.Timestamp(int(epoch), unit="s", tz="UTC").tz_convert(IST).tz_localize(None)


def classify(status: int, body: Any, headers: Mapping[str, str]) -> None:
    code = message = None
    if isinstance(body, dict):
        errs = body.get("errors")
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            code, message = errs[0].get("errorCode"), errs[0].get("message") or ""
        if body.get("status") == "success" and status < 400:
            return
        if body.get("status") == "error" or code:
            http_failure(status if status >= 400 else 400, code, message or "", headers, auth_codes=AUTH_CODES, rate_codes=RATE_CODES)
    if status >= 400:
        http_failure(status, code, message or "", headers, auth_codes=AUTH_CODES, rate_codes=RATE_CODES)
