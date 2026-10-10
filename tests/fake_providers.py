"""Local, SYNTHETIC stand-ins for the FYERS v3 and Upstox v3 historical-candle endpoints.

They serve candles from the same deterministic generator as ``fake_dhan`` (so the three providers can be compared bar for
bar) in each provider's OWN wire format: FYERS ``{"s": "ok", "candles": [[epoch, o, h, l, c, v]]}`` and Upstox
``{"status": "success", "data": {"candles": [[iso-with-offset, o, h, l, c, v, oi]]}}`` (newest first).

The behaviours they encode (inclusive or exclusive 'to' date, per-request range caps, earliest dates, error shapes) are
assumptions standing in for the real services. Nothing here contacts a provider.
"""
from __future__ import annotations

import json
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from fake_dhan import IST, SimInstrument, bars_for_day, daily_for_day


def _candles(inst: SimInstrument, intraday: bool, first: date, last: date, *, earliest: date | None) -> list[dict]:
    out, day = [], first
    while day <= last:
        if earliest is None or day >= earliest:
            if intraday:
                out += bars_for_day(inst, day)
            else:
                c = daily_for_day(inst, day)
                if c:
                    out.append(c)
        day += timedelta(days=1)
    return out


class _Base:
    def __init__(self) -> None:
        self.rules: list[dict] = []
        self.log: list[dict] = []
        self.delay = 0.0
        self._lock = threading.Lock()

    def fail(self, *, times: int, status: int, match=lambda q: True, body=None, headers=None) -> None:
        self.rules.append({"times": times, "status": status, "match": match, "body": body, "headers": headers or {}})

    def requests(self, contains: str = "") -> list[dict]:
        return [r for r in self.log if contains in r["path"]]

    def _take_rule(self, query: dict):
        with self._lock:
            rule = next((r for r in self.rules if r["times"] > 0 and r["match"](query)), None)
            if rule:
                rule["times"] -= 1
            return rule

    def _send(self, h, status: int, body, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer._dispatch(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeFyers(_Base):
    """GET /data/history and GET /api/v3/profile."""

    def __init__(self, instruments: dict[str, SimInstrument], *, app_id: str = "TEST-100", token: str = "fyers-test-token",
                 inclusive_to: bool = True, daily_epoch: str = "ist_midnight", earliest_daily: date | None = None,
                 earliest_intraday: date | None = None, max_days_intraday: int = 100, max_days_daily: int = 366,
                 auth_error_style: str = "http401") -> None:
        super().__init__()
        self.instruments, self.app_id, self.token = instruments, app_id, token
        self.inclusive_to, self.daily_epoch = inclusive_to, daily_epoch
        self.earliest = {"daily": earliest_daily, "15min": earliest_intraday}
        self.max_days = {"daily": max_days_daily, "15min": max_days_intraday}
        self.auth_error_style = auth_error_style

    def _dispatch(self, h) -> None:
        url = urlparse(h.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if self.delay:
            _time.sleep(self.delay)
        with self._lock:
            self.log.append({"path": url.path, "query": q, "auth": h.headers.get("Authorization")})
        if h.headers.get("Authorization") != f"{self.app_id}:{self.token}":
            if self.auth_error_style == "http401":
                return self._send(h, 401, {"s": "error", "code": -16, "message": "Could not authenticate the user"})
            return self._send(h, 200, {"s": "error", "code": -16, "message": "Could not authenticate the user"})
        rule = self._take_rule(q)
        if rule:
            return self._send(h, rule["status"], rule["body"] or {"s": "error", "code": -99, "message": "injected failure"}, rule["headers"])
        if url.path == "/api/v3/profile":
            return self._send(h, 200, {"s": "ok", "code": 200, "data": {"fy_id": "XX00000"}})
        if url.path != "/data/history":
            return self._send(h, 404, {"s": "error", "code": -1, "message": "not found"})
        sym = q.get("symbol", "")
        inst = self.instruments.get(sym)
        if inst is None:
            return self._send(h, 200, {"s": "error", "code": -300, "message": "Please provide a valid symbol"})
        tfname = "daily" if q.get("resolution") in ("1D", "D", "Day") else "15min"
        if q.get("resolution") not in ("1D", "D", "Day", "15"):
            return self._send(h, 200, {"s": "error", "code": -300, "message": "bad resolution"})
        first, last = date.fromisoformat(q["range_from"]), date.fromisoformat(q["range_to"])
        if (last - first).days > self.max_days[tfname]:
            return self._send(h, 200, {"s": "error", "code": -300, "message": f"range exceeds {self.max_days[tfname]} days"})
        if not self.inclusive_to:
            last -= timedelta(days=1)
        rows = _candles(inst, tfname == "15min", first, last, earliest=self.earliest[tfname])
        if not rows:
            return self._send(h, 200, {"s": "no_data", "candles": [], "message": "no data"})
        out = []
        for c in rows:
            ts = c["ts"]
            if tfname == "daily" and self.daily_epoch == "utc_midnight":
                ts = datetime.combine(ts.astimezone(IST).date(), time.min).replace(tzinfo=timezone.utc)
            out.append([int(ts.timestamp()), c["open"], c["high"], c["low"], c["close"], c["volume"]])
        self._send(h, 200, {"s": "ok", "candles": out})


class FakeUpstox(_Base):
    """GET /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}."""

    def __init__(self, instruments_by_isin: dict[str, SimInstrument], *, token: str = "upstox-test-token",
                 inclusive_to: bool = True, earliest_daily: date = date(2000, 1, 1), earliest_intraday: date = date(2022, 1, 1),
                 max_days_minutes: int = 31, max_days_days: int = 3650) -> None:
        super().__init__()
        self.instruments, self.token, self.inclusive_to = instruments_by_isin, token, inclusive_to
        self.earliest = {"days": earliest_daily, "minutes": earliest_intraday}
        self.max_days = {"minutes": max_days_minutes, "days": max_days_days}

    def _dispatch(self, h) -> None:
        url = urlparse(h.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        if self.delay:
            _time.sleep(self.delay)
        with self._lock:
            self.log.append({"path": url.path, "auth": h.headers.get("Authorization")})
        if h.headers.get("Authorization") != f"Bearer {self.token}":
            return self._send(h, 401, {"status": "error", "errors": [{"errorCode": "UDAPI100050", "message": "Invalid token used to access API"}]})
        q = {"key": parts[2] if len(parts) > 2 else ""}
        rule = self._take_rule(q)
        if rule:
            return self._send(h, rule["status"], rule["body"] or {"status": "error", "errors": [{"errorCode": "UDAPI10005", "message": "Too many requests"}]}, rule["headers"])
        # /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}
        if len(parts) != 7 or parts[:2] != ["v3", "historical-candle"]:
            return self._send(h, 404, {"status": "error", "errors": [{"errorCode": "UDAPI100060", "message": "Resource not Found"}]})
        key, unit, interval, to_d, from_d = parts[2:7]
        inst = self.instruments.get(key.split("|", 1)[-1]) if key.startswith("NSE_EQ|") else None
        if inst is None:
            return self._send(h, 400, {"status": "error", "errors": [{"errorCode": "UDAPI100011", "message": "Invalid Instrument key"}]})
        if (unit, interval) not in (("days", "1"), ("minutes", "15")):
            return self._send(h, 400, {"status": "error", "errors": [{"errorCode": "UDAPI1001", "message": "bad unit"}]})
        first, last = date.fromisoformat(from_d), date.fromisoformat(to_d)
        if (last - first).days > self.max_days[unit]:
            return self._send(h, 400, {"status": "error", "errors": [{"errorCode": "UDAPI100072", "message": "range too large"}]})
        if not self.inclusive_to:
            last -= timedelta(days=1)
        rows = _candles(inst, unit == "minutes", first, last, earliest=self.earliest[unit])
        out = [[c["ts"].isoformat(), c["open"], c["high"], c["low"], c["close"], c["volume"], 0] for c in rows]
        out.reverse()                                      # Upstox answers newest first
        self._send(h, 200, {"status": "success", "data": {"candles": out}})
