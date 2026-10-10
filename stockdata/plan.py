"""The request plan of a download: what the command line prints for ``--dry-run`` and what the web application
previews. Built from the same functions the downloader uses to walk its windows, so a preview cannot drift
from what a real run would request.

Read-only: it never sends a request and, with ``persist_history_start=False``, never writes to the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from stockdata import config
from stockdata.config import Settings
from stockdata.db import CandleDB
from stockdata.downloader import RunOptions, plan_instrument_windows, resolve_history_start, select_instruments
from stockdata.instruments import Instrument
from stockdata.timeutil import cutoff_exclusive, ist_now, to_midnight
from stockdata.windows import Window


class PlanError(ValueError):
    """The requested selection or range is invalid (the message is safe to show to the user)."""


@dataclass
class InstrumentPlan:
    instrument: Instrument
    windows: list[Window]
    skipped_failed: int               # failed windows that a run would skip (unless retry_failed)

    @property
    def requests(self) -> int:
        return len(self.windows) - self.skipped_failed


@dataclass
class DownloadPlan:
    timeframe: str
    opts: RunOptions
    instruments: list[Instrument]
    per_instrument: list[InstrumentPlan] = field(default_factory=list)
    windows_computed: bool = False

    @property
    def requests(self) -> int:
        """Minimum API attempts: one per window to request, before any retry."""
        return sum(p.requests for p in self.per_instrument)

    @property
    def skipped_failed(self) -> int:
        return sum(p.skipped_failed for p in self.per_instrument)

    @property
    def instruments_with_work(self) -> int:
        return sum(1 for p in self.per_instrument if p.requests)


def build_download_plan(db: CandleDB, *, settings: Settings, symbols: list[str] | None = None,
                        security_ids: list[int] | None = None, select_all: bool = False,
                        limit: int | None = None, start_date: date | None = None, end_date: date | None = None,
                        window_days: int | None = None, overlap_days: int = config.DEFAULT_OVERLAP_DAYS,
                        refetch: bool = False, retry_failed: bool = False,
                        empty_error_codes: frozenset[str] = frozenset(), persist_history_start: bool = False,
                        with_windows: bool = True) -> DownloadPlan:
    tf = db.tf
    insts = select_instruments(db, symbols=symbols, security_ids=security_ids, select_all=select_all, limit=limit)
    cutoff = cutoff_exclusive(ist_now(), config.FINALIZE_AFTER_HOUR_IST)
    if end_date:
        end = to_midnight(end_date)
        if end > cutoff:
            raise PlanError(f"end date {end_date} is later than the last final day boundary "
                            f"{cutoff:%Y-%m-%d} (exclusive); today's candles are not final yet")
        cutoff = end
    history_start = resolve_history_start(tf, db, start_date, cutoff, persist=persist_history_start)
    days = window_days or tf.default_window_days
    if tf.max_window_days and days > tf.max_window_days:
        raise PlanError(f"window of {days} days exceeds the {tf.max_window_days}-day cap")
    opts = RunOptions(history_start=history_start, cutoff=cutoff, window_days=days, overlap_days=overlap_days,
                      refetch=refetch, retry_failed=retry_failed, empty_error_codes=empty_error_codes,
                      intraday_date_format=settings.intraday_date_format, client_id=settings.client_id)
    plan = DownloadPlan(timeframe=tf.name, opts=opts, instruments=insts)
    if with_windows:
        for inst in insts:
            windows = plan_instrument_windows(db, inst, opts)
            skipped = 0 if retry_failed else sum(1 for w in windows if db.window_status(inst, w) == "failed")
            plan.per_instrument.append(InstrumentPlan(inst, windows, skipped))
        plan.windows_computed = True
    return plan
