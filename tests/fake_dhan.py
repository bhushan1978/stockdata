"""A local, SYNTHETIC stand-in for the Dhan chart endpoints.

It exists only to exercise the downloader's mechanics (windows, retries,
resume, idempotency, validation) in an environment with no Dhan access.
Everything it returns is generated; the API behaviours it encodes (exclusive
``toDate``, 90-day cap, Unix epoch, midnight-IST daily timestamps) are
*assumptions* to be confirmed against the real API with ``verify-api``.
"""
from __future__ import annotations

import json
import random
import socket
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
HOLIDAYS = {date(2023, 12, 25), date(2024, 1, 26), date(2024, 3, 25), date(2024, 8, 15), date(2024, 10, 2)}
SLOTS = 25  # 09:15 .. 15:15 bar starts


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


@dataclass
class SimInstrument:
    security_id: int
    listing: date
    base: float = 1000.0
    illiquid: bool = False          # ~30% of bars missing (no trades) -> must never be filled
    split_on: date | None = None    # serve ADJUSTED history before this date (corp-action tests)
    split_factor: float = 1.0


def bars_for_day(inst: SimInstrument, d: date) -> list[dict]:
    """Deterministic synthetic 15-minute bars for one trading day."""
    if not is_trading_day(d) or d < inst.listing or d > date.today():
        return []                                    # no data before listing or in the future
    level = inst.base * (1 + 0.1 * __import__("math").sin(d.toordinal() / 50.0))
    bars, prev_close = [], level
    for slot in range(SLOTS):
        rng = random.Random(f"{inst.security_id}|{d.isoformat()}|{slot}")
        o = prev_close
        c = o * (1 + rng.uniform(-0.004, 0.004))
        h = max(o, c) * (1 + rng.uniform(0, 0.002))
        l = min(o, c) * (1 - rng.uniform(0, 0.002))
        o, c = round(o * 20) / 20, round(c * 20) / 20
        h, l = round(h * 20) / 20, round(l * 20) / 20
        h, l = max(h, o, c), min(l, o, c)
        prev_close = c
        if inst.illiquid and rng.random() < 0.3:
            continue
        vol = 0 if rng.random() < 0.05 else rng.randint(1, 50_000)
        ts = datetime.combine(d, time(9, 15), tzinfo=IST) + timedelta(minutes=15 * slot)
        bars.append({"open": o, "high": h, "low": l, "close": c, "volume": vol, "ts": ts})
    if inst.split_on and d < inst.split_on:  # a source that serves split-adjusted history
        for b in bars:
            for k in ("open", "high", "low", "close"):
                b[k] = round(b[k] * inst.split_factor, 4)
            b["volume"] = int(round(b["volume"] / inst.split_factor))
    return bars


def daily_for_day(inst: SimInstrument, d: date) -> dict | None:
    bars = bars_for_day(inst, d)
    if not bars:
        return None
    return {"open": bars[0]["open"], "high": max(b["high"] for b in bars),
            "low": min(b["low"] for b in bars), "close": bars[-1]["close"],
            "volume": sum(b["volume"] for b in bars),
            "ts": datetime.combine(d, time.min, tzinfo=IST)}


def _parse_dt(text: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(text)


@dataclass
class FakeDhan:
    instruments: dict[int, SimInstrument]
    token: str = "test-token"
    epoch_offset: int = 0            # e.g. 315532800 emulates the pre-v2 1980-based epoch
    inclusive_to: bool = False       # True emulates an API whose toDate is inclusive
    empty_error_code: str | None = None  # serve this error instead of empty arrays
    max_intraday_days: int = 90
    ms_epoch: bool = False
    delay: float = 0.0               # seconds each request takes (lets tests stop/kill a run mid-flight)
    rules: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- control -------------------------------------------------------
    def fail(self, *, times: int, status: int | str, match=lambda p: True, headers=None, body=None):
        """Fail the next ``times`` matching requests (status int, or 'drop' to cut the connection)."""
        self.rules.append({"times": times, "status": status, "match": match,
                           "headers": headers or {}, "body": body})

    def requests(self, path_suffix: str | None = None) -> list[dict]:
        return [r for r in self.log if path_suffix is None or r["path"].endswith(path_suffix)]

    # -- lifecycle -----------------------------------------------------
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                fake._handle(self, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}/v2"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- request handling -----------------------------------------------
    def _send(self, h, status: int, body, headers: dict | None = None, raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def _handle(self, h, payload: dict) -> None:
        path = h.path
        if self.delay:
            _time.sleep(self.delay)
        with self._lock:
            self.log.append({"path": path, "payload": payload, "token_ok": h.headers.get("access-token") == self.token})
            rule = next((r for r in self.rules if r["times"] > 0 and r["match"](payload)), None)
            if rule:
                rule["times"] -= 1
        if h.headers.get("access-token") != self.token:
            return self._send(h, 401, {"errorType": "Invalid_Authentication", "errorCode": "DH-901",
                                       "errorMessage": "Client ID or user generated access token is invalid"})
        if rule:
            if rule["status"] == "drop":
                h.close_connection = True
                h.connection.shutdown(socket.SHUT_RDWR)
                return
            if rule["status"] == "garbage":
                return self._send(h, 200, None, raw=b'{"open": [1, 2')
            if rule["status"] == "empty":      # a well-formed but empty answer (ambiguous: no data? outage?)
                return self._send(h, 200, {k: [] for k in ("open", "high", "low", "close", "volume", "timestamp")})
            return self._send(h, rule["status"], rule["body"] or {"errorType": "Injected", "errorCode": "DH-908",
                                                                  "errorMessage": "injected failure"},
                              rule["headers"])
        try:
            self._serve_candles(h, path, payload)
        except (KeyError, ValueError) as exc:
            self._send(h, 400, {"errorType": "Input_Exception", "errorCode": "DH-905",
                                "errorMessage": f"bad request: {exc}"})

    def _serve_candles(self, h, path: str, p: dict) -> None:
        inst = self.instruments.get(int(p["securityId"]))
        if inst is None or p["exchangeSegment"] != "NSE_EQ" or p["instrument"] != "EQUITY":
            raise ValueError("unknown instrument")
        start, end = _parse_dt(p["fromDate"]), _parse_dt(p["toDate"])
        if end <= start:
            raise ValueError("toDate must be after fromDate")
        intraday = path.endswith("/charts/intraday")
        if intraday:
            if p.get("interval") not in (1, 5, 15, 25, 60, "1", "5", "15", "25", "60"):
                raise ValueError("bad interval")
            if (end - start) > timedelta(days=self.max_intraday_days):
                raise ValueError(f"range exceeds {self.max_intraday_days} days")
        candles = []
        day = start.date()
        while day <= end.date():
            if intraday:
                for b in bars_for_day(inst, day):
                    t = b["ts"].replace(tzinfo=None)
                    if start <= t < end or (self.inclusive_to and t == end):
                        candles.append(b)
            else:
                c = daily_for_day(inst, day)
                if c:
                    d0 = datetime.combine(day, time.min)
                    if start <= d0 < end or (self.inclusive_to and d0 == end):
                        candles.append(c)
            day += timedelta(days=1)
        if not candles and self.empty_error_code:
            return self._send(h, 400, {"errorType": "Data_Error", "errorCode": self.empty_error_code,
                                       "errorMessage": "no data present"})
        scale = 1000 if self.ms_epoch else 1
        body = {k: [c[k] for c in candles] for k in ("open", "high", "low", "close", "volume")}
        body["timestamp"] = [int(c["ts"].timestamp()) * scale + self.epoch_offset for c in candles]
        body["open_interest"] = [0] * len(candles)
        self._send(h, 200, body)
