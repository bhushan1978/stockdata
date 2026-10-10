"""Capability probe: turns a provider's PUBLISHED limits into MEASURED ones, from this installation.

A handful of small, explicit requests for one instrument (about a dozen in total, all counted in the shared request ledger):

* ``recent``       - the last few final days: proves the credential, the instrument mapping and the response format;
* ``max_window``   - a request as long as the longest window this application will ever send: proves that length is accepted
                     (when it is refused, half of it is tried once, so the report says what is actually accepted);
* ``earliest``     - small windows at the provider's documented first date, or, when that is unknown, at a few anchors going
                     back in time until one is empty: proves how far back data really goes for this instrument.

The result says ``accepted`` / ``has data since``; it never claims a limit beyond what was observed (a refused longer window
is not tried, so the true cap may be higher). Nothing is stored in the candle databases: the candles are validated and discarded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from stockdata import config
from stockdata.budget import RequestLimitReached
from stockdata.downloader import RunOptions
from stockdata.providers.base import ProviderAdapter
from stockdata.providers.errors import AuthError, PermanentError, TransientError
from stockdata.timeframes import TIMEFRAMES, Timeframe
from stockdata.timeutil import cutoff_exclusive, ist_now, to_midnight
from stockdata.validation import ValidationError
from stockdata.windows import Window

log = logging.getLogger("stockdata.probe")

DAILY_ANCHORS = (date(2024, 1, 1), date(2020, 1, 1), date(2015, 1, 1), date(2010, 1, 1), date(2005, 1, 1), date(2000, 1, 1))
INTRADAY_ANCHORS = (date(2024, 1, 1), date(2022, 6, 1), date(2021, 1, 1), date(2019, 1, 1), date(2017, 1, 1))
ANCHOR_SPAN_DAYS = 12


@dataclass
class Step:
    name: str
    timeframe: str
    ok: bool
    detail: str
    window: tuple[str, str] | None = None
    candles: int | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "timeframe": self.timeframe, "ok": self.ok, "detail": self.detail,
                "window": list(self.window) if self.window else None, "candles": self.candles}


@dataclass
class ProbeReport:
    provider: str
    instrument: str
    state: str = "ok"                       # ok | auth_failed | stopped (budget / network) | partial
    message: str = ""
    steps: list[Step] = field(default_factory=list)
    verified: dict = field(default_factory=dict)    # tf -> {max_window_days_accepted, has_data_since, empty_before}
    requests_used: int = 0

    def to_dict(self) -> dict:
        return {"provider": self.provider, "instrument": self.instrument, "state": self.state, "message": self.message,
                "steps": [s.to_dict() for s in self.steps], "verified": self.verified, "requests_used": self.requests_used,
                "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _try(adapter: ProviderAdapter, client, inst, tf: Timeframe, window: Window, opts: RunOptions):
    """-> (candle count | None, detail). Raises AuthError / RequestLimitReached to end the probe."""
    try:
        raw = adapter.fetch(client, inst, tf, window, opts)
        v = adapter.to_canonical(tf, raw, window)
        return v.n, ("accepted" if v.n else "accepted, no candles in this window")
    except (AuthError, RequestLimitReached):
        raise
    except PermanentError as exc:
        return None, f"refused: {exc.message}"
    except TransientError as exc:
        return None, f"temporary error: {exc.message}"
    except ValidationError as exc:
        return None, f"answer rejected by validation: {exc}"


def run_probe(adapter: ProviderAdapter, client, inst, *, timeframes: tuple[str, ...] = ("daily", "15min"),
              now: datetime | None = None) -> ProbeReport:
    now = now or ist_now()
    cutoff = cutoff_exclusive(now, config.FINALIZE_AFTER_HOUR_IST)
    rep = ProbeReport(adapter.name, inst.symbol)
    opts = RunOptions(history_start=cutoff - timedelta(days=1), cutoff=cutoff, window_days=1, intraday_date_format="datetime")
    sent0 = getattr(client, "requests_sent", 0)
    try:
        for name in timeframes:
            tf = TIMEFRAMES[name]
            if not adapter.supports(tf):
                rep.steps.append(Step("supported", name, False, f"{adapter.label} does not provide {name} candles"))
                continue
            ver = rep.verified.setdefault(name, {})
            # 1. recent
            span = 30 if not tf.is_intraday else 10
            w = Window(cutoff - timedelta(days=span), cutoff)
            n, detail = _try(adapter, client, inst, tf, w, opts)
            rep.steps.append(Step("recent", name, n is not None and n > 0, detail if n != 0 else "no candles in the last days (holiday period?)",
                                  (f"{w.start:%Y-%m-%d}", f"{w.end:%Y-%m-%d}"), n))
            # 2. longest window this application sends
            days = adapter.window_days(tf)
            if days > span:
                w = Window(cutoff - timedelta(days=days), cutoff)
                n, detail = _try(adapter, client, inst, tf, w, opts)
                if n is None and days > 2:
                    half = days // 2
                    rep.steps.append(Step("max_window", name, False, f"{days} days {detail}", (f"{w.start:%Y-%m-%d}", f"{w.end:%Y-%m-%d}")))
                    w = Window(cutoff - timedelta(days=half), cutoff)
                    n, detail = _try(adapter, client, inst, tf, w, opts)
                    days = half
                rep.steps.append(Step("max_window", name, n is not None, f"{days} days {detail}", (f"{w.start:%Y-%m-%d}", f"{w.end:%Y-%m-%d}"), n))
                if n is not None:
                    ver["max_window_days_accepted"] = days
            elif rep.steps[-1].ok:
                ver["max_window_days_accepted"] = span
            # 3. how far back
            first = adapter.earliest(tf)
            anchors = ([first] if first else list(DAILY_ANCHORS if not tf.is_intraday else INTRADAY_ANCHORS))
            for a in anchors:
                w = Window(to_midnight(a), to_midnight(a) + timedelta(days=ANCHOR_SPAN_DAYS))
                n, detail = _try(adapter, client, inst, tf, w, opts)
                rep.steps.append(Step("earliest", name, bool(n), detail if n != 0 else f"no candles from {a}", (f"{w.start:%Y-%m-%d}", f"{w.end:%Y-%m-%d}"), n))
                if n:
                    ver["has_data_since"] = a.isoformat()
                else:
                    if n == 0:
                        ver["empty_at"] = a.isoformat()
                    break
            if first:                                           # the documented first date: is anything there BEFORE it?
                before = Window(to_midnight(first) - timedelta(days=ANCHOR_SPAN_DAYS * 2), to_midnight(first))
                n, detail = _try(adapter, client, inst, tf, before, opts)
                rep.steps.append(Step("before_earliest", name, n == 0, "nothing before the documented first date" if n == 0 else detail,
                                      (f"{before.start:%Y-%m-%d}", f"{before.end:%Y-%m-%d}"), n))
                if n:
                    ver["data_before_documented_first"] = True
    except AuthError as exc:
        rep.state, rep.message = "auth_failed", f"the credential was rejected ({exc.message}); no further request was made"
    except RequestLimitReached as exc:
        rep.state, rep.message = "stopped", f"the request budget stopped the probe: {exc}"
    rep.requests_used = getattr(client, "requests_sent", 0) - sent0
    if rep.state == "ok" and any(not s.ok for s in rep.steps if s.name in ("recent", "max_window")):
        rep.state, rep.message = "partial", "some probes did not succeed; see the steps"
    return rep
