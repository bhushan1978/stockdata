"""`verify-api`: small controlled LIVE probes that settle what could not be read from the docs.

Nothing here assumes an answer: each probe records what the API actually did,
and the findings tell you which defaults (epoch base, exclusive toDate, window
caps, empty-window behaviour) are confirmed. ~20 requests in total.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from stockdata import config
from stockdata.config import Settings
from stockdata.dhan import DhanClient, DhanError, RateLimiter
from stockdata.instruments import Instrument
from stockdata.timeframes import DAILY, INTRADAY_15, Timeframe
from stockdata.timeutil import IST, cutoff_exclusive, ist_now

EPOCH_OFFSETS = {"unix_1970": 0, "base_1980_(pre-v2)": 315532800}


def _ist(ts: int, offset: int = 0) -> datetime:
    return datetime.fromtimestamp(ts - offset, IST).replace(tzinfo=None)


class Prober:
    def __init__(self, client: DhanClient, inst: Instrument, settings: Settings):
        self.client, self.inst, self.settings = client, inst, settings
        self.log: list[dict] = []
        self.findings: list[str] = []

    def call(self, tf: Timeframe, start: datetime, end: datetime, label: str, *, date_format: str | None = None,
             mutate=None) -> dict:
        payload = tf.payload(self.inst, start, end, intraday_date_format=date_format or self.settings.intraday_date_format,
                             client_id=self.settings.client_id)
        if mutate:
            mutate(payload)
        rec = {"probe": label, "endpoint": tf.endpoint, "from": payload["fromDate"], "to": payload["toDate"]}
        try:
            r = self.client.post(tf.endpoint, payload)
            body = r.data
            ts = body.get("timestamp") if isinstance(body, dict) else None
            rec.update(ok=True, http_status=r.http_status, keys=sorted(body) if isinstance(body, dict) else None,
                       n=len(ts) if isinstance(ts, list) else None, timestamps=ts if isinstance(ts, list) else [],
                       body=body)
        except DhanError as exc:
            rec.update(ok=False, http_status=exc.http_status, error_code=exc.error_code, error=exc.message, n=None,
                       timestamps=[])
        self.log.append({k: v for k, v in rec.items() if k not in ("body",)})
        return rec

    def finding(self, text: str) -> None:
        self.findings.append(text)

    # ------------------------------------------------------------------ probes
    def epoch_base(self, rec: dict, start: datetime, end: datetime, what: str) -> int | None:
        scores = {name: sum(start <= _ist(t, off) < end for t in rec["timestamps"])
                  for name, off in EPOCH_OFFSETS.items()}
        best = max(scores, key=scores.get)
        total = len(rec["timestamps"])
        self.finding(f"[{what}] epoch base: {best} puts {scores[best]}/{total} timestamps inside the requested window "
                     f"(scores {scores}) -> {'CONFIRMED' if scores[best] == total and total else 'NOT CONFIRMED'}")
        return EPOCH_OFFSETS[best] if total else None

    def run_all(self) -> None:
        cutoff = cutoff_exclusive(ist_now(), config.FINALIZE_AFTER_HOUR_IST)
        # ---- daily
        a_start, a_end = cutoff - timedelta(days=45), cutoff
        a = self.call(DAILY, a_start, a_end, "daily_recent")
        offset = 0
        if a["ok"] and a["n"]:
            offset = self.epoch_base(a, a_start, a_end, "daily") or 0
            times = Counter(_ist(t, offset).strftime("%H:%M:%S") for t in a["timestamps"])
            days = [_ist(t, offset).date() for t in a["timestamps"]]
            self.finding(f"[daily] response keys {a['keys']}; {a['n']} candles; IST time-of-day values {dict(times)}; "
                         f"weekend dates: {sum(d.weekday() >= 5 for d in days)}")
            last = max(days)
            lo = datetime.combine(last - timedelta(days=10), datetime.min.time())
            excl = self.call(DAILY, lo, datetime.combine(last, datetime.min.time()), "daily_to_exclusive")
            incl = self.call(DAILY, lo, datetime.combine(last + timedelta(days=1), datetime.min.time()), "daily_to_plus_one")
            in_excl = excl["ok"] and any(_ist(t, offset).date() == last for t in excl["timestamps"])
            in_incl = incl["ok"] and any(_ist(t, offset).date() == last for t in incl["timestamps"])
            verdict = ("EXCLUSIVE (last day absent when toDate=that day, present when toDate=next day)"
                       if (not in_excl and in_incl) else "INCLUSIVE (toDate day is returned) - the exclusive assumption is WRONG"
                       if in_excl else "inconclusive")
            self.finding(f"[daily] toDate boundary: {verdict}")
        else:
            self.finding(f"[daily] recent window failed or empty: {a.get('error') or 'no candles'}")
        for years in (30,):
            lng = self.call(DAILY, datetime(1990, 1, 1), cutoff, f"daily_range_{years}y")
            if lng["ok"] and lng["n"]:
                first = min(_ist(t, offset) for t in lng["timestamps"])
                self.finding(f"[daily] one request from 1990 accepted: {lng['n']} candles, earliest {first:%Y-%m-%d}; "
                             "(set --window-days accordingly)")
            else:
                self.finding(f"[daily] a 1990->today request was rejected ({lng.get('error_code')} {lng.get('error')}); "
                             "lower DEFAULT_DAILY_WINDOW_DAYS")
        # ---- intraday
        i_start, i_end = cutoff - timedelta(days=10), cutoff
        i = self.call(INTRADAY_15, i_start, i_end, "intraday_recent_datetime", date_format="datetime")
        if i["ok"] and i["n"]:
            offset = self.epoch_base(i, i_start, i_end, "intraday") or 0
            bars = [_ist(t, offset) for t in i["timestamps"]]
            per_day = Counter(b.date() for b in bars)
            firsts = Counter(min(b for b in bars if b.date() == d).strftime("%H:%M") for d in per_day)
            lasts = Counter(max(b for b in bars if b.date() == d).strftime("%H:%M") for d in per_day)
            grid = all((b.hour * 60 + b.minute - 555) % 15 == 0 for b in bars)
            self.finding(f"[intraday] interval=15 accepted; bars/session {dict(Counter(per_day.values()))}; first bar "
                         f"{dict(firsts)}, last bar {dict(lasts)}; all on 09:15 + 15-min grid: {grid} "
                         "(first bar 09:15 => timestamps mark the bar START)")
            j = self.call(INTRADAY_15, i_start, i_end, "intraday_recent_date_only", date_format="date")
            k = self.call(INTRADAY_15, i_start, i_end, "intraday_interval_as_string", mutate=lambda p: p.update(interval="15"))
            self.finding(f"[intraday] date-only fromDate/toDate accepted: {j['ok']} (same bars: {j['ok'] and j['timestamps'] == i['timestamps']}); "
                         f"interval as string accepted: {k['ok']} (same bars: {k['ok'] and k['timestamps'] == i['timestamps']})")
        else:
            self.finding(f"[intraday] recent window failed or empty: {i.get('error_code')} {i.get('error') or 'no candles'}")
        accepted = {}
        for days in (89, 90, 91, 100):
            r = self.call(INTRADAY_15, cutoff - timedelta(days=days), cutoff, f"intraday_span_{days}d")
            accepted[days] = r["ok"]
        self.finding(f"[intraday] span acceptance by days: {accepted} -> the documented cap of 90 days is "
                     f"{'CONFIRMED' if accepted[90] and not accepted[91] else 'NOT CONFIRMED (see the raw log)'}")
        five = ist_now().replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
        old_start = five.replace(year=five.year - config.INTRADAY_HISTORY_YEARS) - timedelta(days=60)
        old = self.call(INTRADAY_15, old_start, old_start + timedelta(days=89), "intraday_five_years_ago")
        if old["ok"] and old["n"]:
            earliest = min(_ist(t, offset) for t in old["timestamps"])
            self.finding(f"[intraday] window starting {old_start:%Y-%m-%d} (5y + 60d ago) returned {old['n']} bars, "
                         f"earliest {earliest:%Y-%m-%d}")
        else:
            self.finding(f"[intraday] window 5y+60d ago: {old.get('error_code') or ''} {old.get('error') or 'no bars'} "
                         "(history depth limit)")
        # ---- empty-window behaviour
        fut = cutoff + timedelta(days=30)
        for tf in (DAILY, INTRADAY_15):
            e = self.call(tf, fut, fut + timedelta(days=30), f"{tf.name}_future_window")
            self.finding(f"[{tf.name}] a window with no data returns: ok={e['ok']} http={e['http_status']} "
                         f"n={e['n']} code={e.get('error_code')} msg={e.get('error')!r} "
                         "(if it is an error code, only then consider --empty-error-codes)")


def run(args, settings: Settings) -> int:
    from stockdata.cli import db_path
    token = settings.require_token()
    sec = args.security_id
    if sec is None:
        p = db_path(settings, DAILY)
        if p.exists():
            from stockdata.db import CandleDB
            with CandleDB(DAILY, p, read_only=True) as db:
                found = db.get_instruments(symbols=[args.symbol])
                sec = found[0].security_id if found else None
    if sec is None:
        raise SystemExit("unknown security id: pass --security-id, or run `instruments load` so the symbol resolves")
    client = DhanClient(token, settings.base_url, client_id=settings.client_id,
                        limiter=RateLimiter(min(settings.rps, 2.0)), max_attempts=3)
    pr = Prober(client, Instrument("NSE_EQ", sec, args.symbol, "EQUITY"), settings)
    pr.run_all()
    out = settings.data_dir / "reports"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"verify_api_{datetime.now():%Y%m%d_%H%M%S}.json"
    f.write_text(json.dumps({"symbol": args.symbol, "security_id": sec, "findings": pr.findings,
                             "requests": pr.log}, indent=2, default=str))
    print("\n".join(pr.findings))
    print(f"\nraw request/response summary written to {f}")
    return 0
