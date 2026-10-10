"""FYERS API v3 adapter (historical candles).

Confirmed from FYERS' official Python client (``fyers-apiv3`` 3.1.19) and therefore tagged ``sdk_confirmed``:
  * data host ``https://api-t1.fyers.in/data``, endpoint ``GET /history``;
  * parameters ``symbol`` (e.g. ``NSE:SBIN-EQ``), ``resolution`` ('Day'/'1D', '1', '2', '3', '5', '10', '15', '20', '30', '60',
    '120', '240'), ``date_format`` (0 = epoch, 1 = ``yyyy-mm-dd``), ``range_from``, ``range_to``, ``cont_flag``;
  * authentication ``Authorization: <app id>:<access token>`` with ``version: 3``; profile at ``/api/v3/profile``.

NOT confirmable from here and therefore tagged ``documented_unverified`` or ``unknown``: the per-request date caps, the
request quotas, how far back history goes, whether candles are corporate-action adjusted, and the exact error codes. The
capability probe (Providers screen) replaces those with measured facts; until then the planner stays conservative.

A FYERS access token is obtained by an interactive browser login and is documented to be short-lived, so unattended use
cannot be promised: when the token has expired the application shows 'action required' instead of failing silently.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping
from urllib.parse import urlparse

import pandas as pd

from stockdata.instruments import Instrument
from stockdata.providers import base
from stockdata.providers.base import (CredentialField, Capabilities, AuthRequirement, ConnectionResult, DOCUMENTED, RateLimit,
                                      RawResponse, SDK_CONFIRMED, TimeframeSupport, UNKNOWN, canonical_body, drop_extra_day,
                                      sort_oldest_first)
from stockdata.providers.errors import AuthError, PermanentError, ProviderSetupError
from stockdata.providers.http import ProviderClient, http_failure, require_safe_url
from stockdata.timeutil import IST, epoch_to_ist_naive
from stockdata.validation import Validated, validate_response

DATA_HOST = "https://api-t1.fyers.in"
APP_ID_KEY, TOKEN_KEY = "FYERS_APP_ID", "FYERS_ACCESS_TOKEN"
BASE_URL_KEY = "FYERS_BASE_URL"                      # tests point this at a local fake server
RESOLUTION = {"daily": "1D", "15min": "15"}
AUTH_CODES = frozenset({"-8", "-15", "-16", "-17"})   # token expired / invalid (as documented; unverified here)
TRANSIENT_CODES = frozenset({"-429"})


class FyersAdapter(base.ProviderAdapter):
    name = "fyers"
    label = "FYERS (API v3)"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            provider=self.name, label=self.label,
            timeframes={
                "daily": TimeframeSupport(True, "1D", 366, None, DOCUMENTED,
                                          "range cap and earliest date are the published values as remembered; unverified"),
                "15min": TimeframeSupport(True, "15", 100, None, DOCUMENTED,
                                          "range cap is the published value as remembered; the earliest date is unknown until probed"),
            },
            rate_limit=RateLimit(per_second=10, per_minute=200, per_day=100_000, operating_day_cap=9_000, status=DOCUMENTED,
                                 note="published per-second / per-minute / per-day quotas as remembered; a community report "
                                      "mentions a lower daily cap, so the operating daily cap is kept low until verified"),
            auth=AuthRequirement(
                fields=(CredentialField(APP_ID_KEY, "App ID (client id, e.g. ABCD1234E5-100)", secret=False, placeholder="XXXXXXXXXX-100"),
                        CredentialField(TOKEN_KEY, "Access token", secret=True)),
                token_kind="FYERS API v3 access token (generated after an interactive login)",
                lifetime="short-lived (documented as valid for one trading day); a new login is needed to renew it",
                interactive_login=True,
                unattended_note=("Unattended downloads cannot be promised: renewing the token needs a person to log in. "
                                 "When it expires the application shows ACTION REQUIRED and the scheduled run stops safely."),
                warn_before_seconds=2 * 3600,
                how_to_obtain="Create an API app in the FYERS API dashboard, log in once per day to generate the access token, "
                              "then paste it here (it is stored only in the untracked .env file)."),
            adjustment_basis="unknown", adjustment_status=UNKNOWN,
            intraday_polling=True,
            instrument_mapping="NSE:<symbol>-<series> built from the instrument master (NSE equity only); an unmapped or "
                               "rejected symbol is reported per instrument, never guessed",
            sources=("fyers-apiv3 3.1.19 (official client): endpoint, parameters, resolutions, auth header",
                     "published limits as remembered when written: unverified"))

    def token_env_key(self) -> str:
        return TOKEN_KEY

    # ---------------------------------------------------------------------------------- instruments
    def map_instrument(self, inst: Instrument) -> str | None:
        if inst.exchange_segment != "NSE_EQ" or not inst.symbol:
            return None
        series = (inst.series or "EQ").upper()
        return f"NSE:{inst.symbol.upper()}-{series}"

    # ---------------------------------------------------------------------------------------- client
    def make_client(self, env: Mapping[str, str], *, settings, label: str, budget=None, session=None,
                    max_attempts: int = 5, limiter=None) -> ProviderClient:
        app_id, token = env.get(APP_ID_KEY), env.get(TOKEN_KEY)
        if not app_id or not token:
            raise ProviderSetupError("FYERS credentials are not set: FYERS_APP_ID and FYERS_ACCESS_TOKEN are required "
                                     "(Providers screen, or the untracked .env file).")
        base_url = (env.get(BASE_URL_KEY) or DATA_HOST).rstrip("/")
        require_safe_url(base_url)
        lim = limiter or base.MultiWindowLimiter(_windows(self.capabilities().rate_limit, getattr(settings, "rps_override", None)))
        return ProviderClient(self.name, base_url, headers={"Authorization": f"{app_id}:{token}", "version": "3",
                                                            "Accept": "application/json"},
                              limiter=lim, classify=classify, budget=budget, session=session, max_attempts=max_attempts)

    def fetch(self, client: ProviderClient, inst: Instrument, tf, window, opts) -> RawResponse:
        symbol = self.map_instrument(inst)
        if symbol is None:
            raise PermanentError(f"{inst.symbol}: no FYERS symbol can be derived for segment {inst.exchange_segment}",
                                 error_code="unmapped_instrument")
        params = {"symbol": symbol, "resolution": RESOLUTION[tf.name], "date_format": 1, "cont_flag": 1,
                  "range_from": window.start.strftime("%Y-%m-%d"),
                  "range_to": window.end.strftime("%Y-%m-%d")}      # 'to' = window end: covers the last day under either reading
        resp = client.get("/data/history", params)
        return RawResponse(resp.data, resp.http_status, resp.attempts)

    # --------------------------------------------------------------------------------- normalisation
    def to_canonical(self, tf, raw: RawResponse, window) -> Validated:
        from stockdata.validation import ValidationError
        body = raw.body
        if not isinstance(body, dict) or body.get("s") not in ("ok", "no_data"):
            raise ValidationError("malformed_response", f"unexpected FYERS response shape: {_shape(body)}")
        rows = body.get("candles")
        if rows is None and body.get("s") == "no_data":
            rows = []
        if not isinstance(rows, list) or any(not isinstance(r, list) or len(r) < 6 for r in rows):
            raise ValidationError("malformed_response", "FYERS candles are not [time, open, high, low, close, volume] rows")
        rows = sort_oldest_first([list(r[:6]) for r in rows])
        rows = drop_extra_day(rows, window.end, _ist)
        raw_epochs = {}
        if not tf.is_intraday:
            # a daily bar belongs to the IST DATE of its timestamp; validation expects midnight IST, so the epoch used for
            # validation is rebuilt from that date and the provider's own epoch is restored as ``epoch_raw`` afterwards
            fixed = []
            for r in rows:
                day = _ist(r[0]).normalize()
                midnight = int(day.tz_localize(IST).timestamp())
                raw_epochs[midnight] = int(r[0])
                fixed.append([midnight, *r[1:]])
            rows = fixed
        v = validate_response(tf, canonical_body(rows), window)
        if raw_epochs and v.n:
            v.candles["epoch_raw"] = v.candles["epoch_raw"].map(lambda e: raw_epochs.get(int(e), int(e))).astype("int64")
        return v

    def empty_code(self, raw: RawResponse) -> str | None:
        return "no_data" if isinstance(raw.body, dict) and raw.body.get("s") == "no_data" else None

    # ------------------------------------------------------------------------------ connection test
    def test_connection(self, client: ProviderClient) -> ConnectionResult:
        try:
            resp = client.get("/api/v3/profile")
        except AuthError as exc:
            return ConnectionResult(False, "auth_failed", f"FYERS rejected the credential ({exc.message}); generate a new access token",
                                    requests_used=client.requests_sent)
        except PermanentError as exc:
            return ConnectionResult(False, "rejected", f"FYERS refused the profile request: {exc.message}", requests_used=client.requests_sent)
        except Exception as exc:                      # noqa: BLE001 - network / budget problems are reported, not raised
            return ConnectionResult(False, "network", f"could not complete the test: {type(exc).__name__}: {exc}", requests_used=client.requests_sent)
        ok = isinstance(resp.data, dict) and resp.data.get("s") == "ok"
        return ConnectionResult(ok, "ok" if ok else "rejected", "FYERS accepted the credential" if ok else "unexpected profile answer",
                                requests_used=client.requests_sent)


# -------------------------------------------------------------------------------------------- helpers
def _windows(limit: RateLimit, rps_override: float | None) -> list[tuple[int, float]]:
    if rps_override:
        return limit.windows(fraction=rps_override / (limit.per_second or 1))
    return limit.windows(fraction=0.6)


def _ist(epoch) -> pd.Timestamp:
    return pd.Timestamp(int(epoch), unit="s", tz="UTC").tz_convert(IST).tz_localize(None)


def _shape(body: Any) -> str:
    return repr(sorted(body)) if isinstance(body, dict) else type(body).__name__


def classify(status: int, body: Any, headers: Mapping[str, str]) -> None:
    """FYERS answers 200 with s='error' for many failures, so the body is inspected as well as the HTTP status."""
    code = message = None
    if isinstance(body, dict):
        raw_code = body.get("code")
        code = str(raw_code) if raw_code not in (None, "") else None
        message = body.get("message") or ""
        if body.get("s") in ("ok", "no_data") and status < 400:
            return
        if body.get("s") == "error" or code:
            if code in ("-429", "429"):
                http_failure(429, code, message, headers)
            http_failure(status if status >= 400 else 400, code, message, headers, auth_codes=AUTH_CODES, transient_codes=TRANSIENT_CODES)
    if status >= 400:
        http_failure(status, code, message or "", headers, auth_codes=AUTH_CODES)
