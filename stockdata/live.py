"""Live / current data: keeping existing datasets up to date WITHOUT downloading history again.

This is a different workflow from the historical download (``stockdata download``), not the same operation with a
different label:

* **post-close update** (``mode='post_close'``): from each instrument's last covered candle (minus a small overlap that
  detects late revisions) up to the last FINAL day boundary. Daily and 15-minute candles are committed through the normal
  transactional window commit, so checkpoints, validation, provenance and the shared request ledger apply unchanged.
  An instrument with no stored candles is reported as ``needs_backfill`` and is NOT requested (that would be a history
  download); an instrument the provider cannot identify is skipped; a repeatedly failing one is skipped until retried.
* **current-session polling** (``mode='current'``, intraday only): every N seconds the completed bars of today are fetched
  and stored as PROVISIONAL bars in a separate table of the same database. They are never part of the canonical candle
  table, so a backtest or an adjusted view can never see a partial day; the post-close update commits the final candles
  and the provisional rows are then deleted. This is repeated polling of delayed historical candles, not a live stream.

Both are idempotent: repeating either never creates a duplicate candle (the candle key is unique, a re-fetched candle that
differs is recorded as a revision and never overwrites).
"""
from __future__ import annotations

import logging
import time as _time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable

import duckdb
import pandas as pd

from stockdata import config
from stockdata.budget import RequestLimitReached
from stockdata.db import CandleDB
from stockdata.downloader import Downloader, RunAborted, RunOptions, STOP_MESSAGE, limit_abort
from stockdata.instruments import Instrument
from stockdata.providers.base import ProviderAdapter
from stockdata.providers.errors import AuthError, PermanentError, TransientError
from stockdata.timeutil import SESSION_CLOSE, SESSION_OPEN, cutoff_exclusive, ist_now, to_midnight, utcnow
from stockdata.validation import ValidationError
from stockdata.windows import Window, chunk_backward

log = logging.getLogger("stockdata.live")

POST_CLOSE, CURRENT = "post_close", "current"
MAX_CONSECUTIVE_FAILURES = 5
MIN_POLL_SECONDS = 60
CLOSE_GRACE_MINUTES = 20


# ------------------------------------------------------------------------------------------ post-close
@dataclass
class UpdateOptions:
    provider: str
    overlap_days: int = config.DEFAULT_OVERLAP_DAYS
    seed_days: int | None = None            # opt-in: give an instrument with no candles this many recent days (not history)
    finalize_after_hour: int = config.FINALIZE_AFTER_HOUR_IST
    retry_failed: bool = False              # also retry instruments that kept failing
    min_interval_minutes: int = 0           # skip an instrument updated successfully within this many minutes
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    intraday_date_format: str = "datetime"
    client_id: str | None = None


@dataclass
class UpdatePlan:
    timeframe: str
    provider: str
    cutoff: datetime
    to_update: list[Instrument] = field(default_factory=list)
    windows: dict[tuple[str, int], list[Window]] = field(default_factory=dict)
    up_to_date: list[Instrument] = field(default_factory=list)
    recent: list[Instrument] = field(default_factory=list)            # updated successfully a moment ago
    needs_backfill: list[Instrument] = field(default_factory=list)
    unmapped: list[Instrument] = field(default_factory=list)
    skipped_failing: list[Instrument] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return sum(len(w) for w in self.windows.values())

    def summary(self) -> dict:
        return {"to_update": len(self.to_update), "up_to_date": len(self.up_to_date), "recently_updated": len(self.recent),
                "needs_backfill": len(self.needs_backfill), "unmapped": len(self.unmapped),
                "skipped_failing": len(self.skipped_failing), "requests": self.requests}


def plan_update(db: CandleDB, adapter: ProviderAdapter, instruments: list[Instrument], opts: UpdateOptions, *,
                now: datetime | None = None) -> UpdatePlan:
    """Read-only: which instruments need what, and exactly which windows would be requested."""
    tf = db.tf
    now = now or ist_now()
    cutoff = cutoff_exclusive(now, opts.finalize_after_hour)
    plan = UpdatePlan(tf.name, adapter.name, cutoff)
    days = adapter.window_days(tf)
    first = adapter.earliest(tf)
    try:
        state = {(r[0], r[1]): r[2:] for r in db.con.execute(
            "SELECT exchange_segment, security_id, last_success_at_utc, consecutive_failures FROM update_state WHERE mode = ?",
            [POST_CLOSE]).fetchall()}
    except duckdb.CatalogException:                       # a read-only open of a database that predates update tracking
        state = {}
    for inst in instruments:
        if adapter.map_instrument(inst) is None:
            plan.unmapped.append(inst)
            continue
        last_ok, fails = state.get((inst.exchange_segment, inst.security_id), (None, 0))
        if fails >= opts.max_consecutive_failures and not opts.retry_failed:
            plan.skipped_failing.append(inst)
            continue
        if last_ok and opts.min_interval_minutes and utcnow() - last_ok < timedelta(minutes=opts.min_interval_minutes):
            plan.recent.append(inst)
            continue
        covered = db.covered_intervals(inst)
        if covered:
            start = max(b for _, b in covered) - timedelta(days=opts.overlap_days)
        elif opts.seed_days:
            start = cutoff - timedelta(days=opts.seed_days)
        else:
            plan.needs_backfill.append(inst)
            continue
        if first is not None and start < to_midnight(first):
            start = to_midnight(first)
        start = to_midnight(start)
        if start >= cutoff:
            plan.up_to_date.append(inst)
            continue
        plan.to_update.append(inst)
        plan.windows[(inst.exchange_segment, inst.security_id)] = chunk_backward(start, cutoff, days)
    if plan.needs_backfill:
        plan.notes.append(f"{len(plan.needs_backfill)} instrument(s) have no stored candles: use the Historical download to "
                          "backfill them first (an update never downloads history)")
    if plan.unmapped:
        plan.notes.append(f"{len(plan.unmapped)} instrument(s) have no {adapter.label} identifier and are skipped")
    if plan.skipped_failing:
        plan.notes.append(f"{len(plan.skipped_failing)} instrument(s) failed {opts.max_consecutive_failures} updates in a row and are "
                          "skipped until you use Retry Failed")
    return plan


def run_update(db: CandleDB, client, adapter: ProviderAdapter, plan: UpdatePlan, opts: UpdateOptions, *, control=None,
               command: str = "") -> dict:
    """Execute a plan through the shared Downloader (budget, stop requests, validation, atomic commit, provenance)."""
    tf = db.tf
    planner = lambda _db, inst, _opts: plan.windows.get((inst.exchange_segment, inst.security_id), [])     # noqa: E731
    run_opts = RunOptions(history_start=plan.cutoff - timedelta(days=1), cutoff=plan.cutoff, window_days=adapter.window_days(tf),
                          overlap_days=0, retry_failed=True, intraday_date_format=opts.intraday_date_format,
                          client_id=opts.client_id, provider=adapter.name, planner=planner)
    if control is not None:
        control.report("plan", instruments=len(plan.to_update), windows=plan.requests)
    stats = Downloader(db, client, control=control, provider=adapter).run(
        plan.to_update, run_opts, command=command or f"update {tf.name} --provider {adapter.name}")
    outcome = Counter()
    for res in stats.get("results", []):
        inst = next((i for i in plan.to_update if i.security_id == res.security_id), None)
        if inst is None:
            continue
        bad = res.windows["failed"] or res.windows["retriable"]
        last = db.con.execute(f"SELECT max({tf.key_col}) FROM {tf.table} WHERE exchange_segment = ? AND security_id = ?",
                              [inst.exchange_segment, inst.security_id]).fetchone()[0]
        db.set_update_state(inst, mode=POST_CLOSE, provider=adapter.name, status="failed" if bad else "ok",
                            error=res.last_failure if bad else None, last_key=last)
        outcome["failed" if bad else "updated"] += 1
    for inst in plan.up_to_date:
        db.set_update_state(inst, mode=POST_CLOSE, provider=adapter.name, status="up_to_date")
    stats["finalized_provisional"] = db.finalize_provisional()
    stats["updated"], stats["instruments_failed"] = outcome["updated"], outcome["failed"]
    stats["plan"] = plan.summary()
    return stats


# --------------------------------------------------------------------------------------- current session
def ceil_to_interval(moment: datetime, minutes: int) -> datetime:
    base = moment.replace(second=0, microsecond=0)
    extra = (minutes - base.minute % minutes) % minutes
    return base + timedelta(minutes=extra if extra else minutes)


def complete_bars(candles: pd.DataFrame, key: str, interval: int, now: datetime, lag_minutes: int = 1) -> pd.DataFrame:
    """Only bars that have FINISHED (start + interval <= now - lag). A bar still forming is never stored."""
    if candles.empty:
        return candles
    done = candles[key] + pd.Timedelta(minutes=interval) <= pd.Timestamp(now - timedelta(minutes=lag_minutes))
    return candles[done].reset_index(drop=True)


@dataclass
class PollOptions:
    provider: str
    poll_seconds: int = 300
    until_close: bool = True
    max_cycles: int | None = None
    lag_minutes: int = 1
    wait_for_open: bool = True
    intraday_date_format: str = "datetime"
    client_id: str | None = None


def session_bounds(day) -> tuple[datetime, datetime]:
    return datetime.combine(day, SESSION_OPEN), datetime.combine(day, SESSION_CLOSE) + timedelta(minutes=CLOSE_GRACE_MINUTES)


def poll_current(db: CandleDB, client, adapter: ProviderAdapter, instruments: list[Instrument], opts: PollOptions, *,
                 control=None, clock: Callable[[], datetime] = lambda: ist_now().replace(tzinfo=None),
                 sleep: Callable[[float], None] = _time.sleep) -> dict:
    """Repeatedly fetch today's completed 15-minute bars into the provisional table until the session ends or a stop is
    requested. Returns a summary; never raises for a provider problem (it is counted and the poll goes on, except for a
    rejected credential or an exhausted request budget, which end the poll)."""
    tf = db.tf
    if not tf.is_intraday:
        raise ValueError("current-session polling applies to intraday timeframes only; a daily candle is final after the close")
    if opts.poll_seconds < MIN_POLL_SECONDS:
        raise ValueError(f"the polling interval must be at least {MIN_POLL_SECONDS} seconds")
    stats: Counter = Counter()
    status, note = "completed", ""
    if control is not None:
        control.report("phase", phase="running")
    run_id = db.start_run(f"update {tf.name} --mode current --provider {adapter.name}")
    sent_before = getattr(client, "requests_sent", 0)
    try:
        while True:
            now = clock()
            if now.weekday() >= 5:
                status, note = "market_closed", "weekend: no session today"
                break
            open_t, end_t = session_bounds(now.date())
            if now < open_t:
                if not opts.wait_for_open:
                    status, note = "market_closed", "the session has not started"
                    break
                _sleep_until(open_t, clock, sleep, control)
                if control is not None and control.should_stop():
                    raise RunAborted(STOP_MESSAGE, status="stopped_by_user")
                continue
            if now > end_t and stats["cycles"]:
                break
            _poll_cycle(db, client, adapter, instruments, opts, stats, now, control)
            stats["cycles"] += 1
            if not opts.until_close or (opts.max_cycles and stats["cycles"] >= opts.max_cycles) or now > end_t:
                break
            _sleep_for(opts.poll_seconds, sleep, control)
            if control is not None and control.should_stop():
                raise RunAborted(STOP_MESSAGE, status="stopped_by_user")
    except RunAborted as exc:
        status, note = exc.status, str(exc)
    finally:
        stats["requests_made"] = getattr(client, "requests_sent", 0) - sent_before
        db.finish_run(run_id, status, {"instruments": len(instruments), "attempted": stats["polled"], "complete": stats["polled"],
                                       "failed": stats["failed"], "rows_inserted": stats["bars"]}, note)
        if control is not None:
            control.report("phase", phase="finished")
    stats["status"], stats["note"] = status, note
    return stats


def _poll_cycle(db, client, adapter, instruments, opts: PollOptions, stats: Counter, now: datetime, control) -> None:
    tf = db.tf
    window = Window(to_midnight(now), ceil_to_interval(now, tf.interval))
    fetch_opts = RunOptions(history_start=window.start, cutoff=window.end, window_days=1,
                            intraday_date_format=opts.intraday_date_format, client_id=opts.client_id)
    if control is not None:
        control.report("plan", instruments=len(instruments), windows=len(instruments))
    for n, inst in enumerate(instruments, 1):
        if control is not None and control.should_stop():
            raise RunAborted(STOP_MESSAGE, status="stopped_by_user")
        if control is not None:
            control.report("instrument_start", index=n, total=len(instruments), symbol=inst.symbol, security_id=inst.security_id)
        outcome, message, bars = "complete", None, 0
        try:
            raw = adapter.fetch(client, inst, tf, window, fetch_opts)
            v = adapter.to_canonical(tf, raw, window)
            done = complete_bars(v.candles, tf.key_col, tf.interval, now, opts.lag_minutes)
            bars = db.upsert_provisional(inst, done, adapter.name)
            db.set_update_state(inst, mode=CURRENT, provider=adapter.name, status="ok",
                                last_key=done[tf.key_col].max().to_pydatetime() if len(done) else None)
            stats["bars"] += bars
            stats["polled"] += 1
        except RequestLimitReached as exc:
            raise limit_abort(exc) from exc
        except AuthError as exc:
            raise RunAborted(f"authentication rejected ({exc.message}); renew the credential and start the poll again ({adapter.label})") from exc
        except (PermanentError, TransientError, ValidationError) as exc:
            outcome, message = "failed", getattr(exc, "message", str(exc))
            stats["failed"] += 1
            db.set_update_state(inst, mode=CURRENT, provider=adapter.name, status="failed", error=message)
            log.warning("%s: poll failed: %s", inst.symbol, message)
        if control is not None:
            control.report("window_done", outcome=outcome, rows_inserted=bars, message=message,
                           requests=getattr(client, "requests_sent", 0))
            control.report("instrument_done")


def _sleep_for(seconds: float, sleep, control) -> None:
    """Sleep in short steps so a stop request is honoured within seconds."""
    left = seconds
    while left > 0:
        step = min(2.0, left)
        sleep(step)
        left -= step
        if control is not None and control.should_stop():
            return


def _sleep_until(target: datetime, clock, sleep, control) -> None:
    while clock() < target:
        _sleep_for(min(30.0, max(1.0, (target - clock()).total_seconds())), sleep, control)
        if control is not None and control.should_stop():
            return
