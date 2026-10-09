"""Download orchestration shared by the daily and 15-minute downloaders.

Per instrument: plan the windows not yet covered, request each one, validate
the response, and commit candles + checkpoint atomically. Nothing is marked
done before its data is committed, so a crash at any point simply leaves the
window pending and a rerun resumes from there.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from stockdata import config
from stockdata.db import CandleDB
from stockdata.dhan import (DhanAuthError, DhanClient, DhanPermanentError, DhanTransientError)
from stockdata.instruments import Instrument
from stockdata.timeframes import Timeframe
from stockdata.timeutil import to_midnight
from stockdata.validation import ValidationError, empty_result, validate_response
from stockdata.windows import Window, plan_windows

log = logging.getLogger("stockdata.download")


class RunAborted(RuntimeError):
    """The whole run must stop (bad token, API down); progress so far is kept."""


@dataclass
class RunOptions:
    history_start: datetime
    cutoff: datetime                      # exclusive upper bound of everything requested
    window_days: int
    overlap_days: int = config.DEFAULT_OVERLAP_DAYS
    refetch: bool = False                 # ignore checkpoints for the range (re-download)
    retry_failed: bool = False            # also retry permanently failed windows
    empty_error_codes: frozenset[str] = frozenset()  # opt-in: API error codes meaning "no data"
    breaker: int = 5                      # consecutive retriable windows before aborting
    intraday_date_format: str = "datetime"
    client_id: str | None = None


@dataclass
class InstrumentResult:
    symbol: str
    security_id: int
    windows: Counter = field(default_factory=Counter)
    rows_inserted: int = 0
    rows_revised: int = 0
    seconds: float = 0.0


def five_years_before(d: datetime) -> datetime:
    try:
        return d.replace(year=d.year - config.INTRADAY_HISTORY_YEARS)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year - config.INTRADAY_HISTORY_YEARS, day=28)


def resolve_history_start(tf: Timeframe, db: CandleDB, explicit: date | None, cutoff: datetime,
                          *, persist: bool = True) -> datetime:
    """Earliest instant to cover. Intraday defaults to five years before the first run
    and is then pinned in download_meta so reruns use identical window boundaries."""
    if explicit is not None:
        return to_midnight(explicit)
    if tf.is_intraday:
        stored = db.get_meta("history_start")
        if stored:
            return datetime.fromisoformat(stored)
        start = five_years_before(cutoff)
        if persist:
            db.set_meta("history_start", start.isoformat(), only_if_missing=True)
        return start
    return to_midnight(config.DAILY_HISTORY_START)


def select_instruments(db: CandleDB, *, symbols: list[str] | None, security_ids: list[int] | None,
                       select_all: bool, limit: int | None = None) -> list[Instrument]:
    """Explicit selection is required; a full-universe run needs ``select_all``."""
    if not (symbols or security_ids or select_all):
        raise SystemExit("Select instruments with --symbols, --security-ids, or --all "
                         "(--all downloads the whole configured universe).")
    found = db.get_instruments(symbols=symbols, security_ids=security_ids)
    if symbols:
        got = {i.symbol.upper() for i in found}
        missing = [s for s in symbols if s.upper() not in got]
        if missing:
            raise SystemExit(f"symbols not in the loaded universe: {', '.join(missing)} "
                             "(run `instruments load` first, or check the universe filter)")
        dup = [s for s, n in Counter(i.symbol.upper() for i in found).items() if n > 1]
        if dup:
            raise SystemExit(f"ambiguous symbols {dup}; select by --security-ids instead")
    if security_ids:
        missing_ids = sorted(set(security_ids) - {i.security_id for i in found})
        if missing_ids:
            raise SystemExit(f"security ids not in the loaded universe: {missing_ids}")
    return found[:limit] if limit else found


class Downloader:
    def __init__(self, db: CandleDB, client: DhanClient):
        self.db, self.client, self.tf = db, client, db.tf

    def run(self, instruments: list[Instrument], opts: RunOptions, *, command: str = "") -> dict:
        if self.tf.max_window_days and opts.window_days > self.tf.max_window_days:
            raise ValueError(f"window of {opts.window_days} days exceeds the {self.tf.max_window_days}-day cap")
        stats: Counter = Counter(instruments=len(instruments))
        results: list[InstrumentResult] = []
        run_id = self.db.start_run(command)
        status, note = "failed", ""
        self._consecutive_retriable = 0
        try:
            for n, inst in enumerate(instruments, 1):
                res = self._download_instrument(n, len(instruments), inst, opts, stats)
                results.append(res)
            status = "completed_with_errors" if stats["failed"] or stats["retriable"] else "completed"
        except RunAborted as exc:
            status, note = "aborted", str(exc)
            log.error("run aborted: %s", exc)
        except KeyboardInterrupt:
            status, note = "aborted", "interrupted by user"
            log.warning("interrupted; progress is checkpointed, rerun the same command to resume")
            raise
        finally:
            self.db.finish_run(run_id, status, stats, note)
        stats["status"] = status
        stats["note"] = note
        stats["results"] = results
        return stats

    # ------------------------------------------------------------------
    def _download_instrument(self, n: int, total: int, inst: Instrument, opts: RunOptions,
                             stats: Counter) -> InstrumentResult:
        t0 = time.monotonic()
        res = InstrumentResult(inst.symbol, inst.security_id)
        covered = [] if opts.refetch else self.db.covered_intervals(inst)
        windows = plan_windows(covered, opts.history_start, opts.cutoff, opts.window_days, 0)
        # The overlap only belongs on a *fresh* forward gap. If one of the plain windows already
        # has a failed/retriable record, keep its exact key so the checkpoint is retried, not orphaned.
        if opts.overlap_days and not opts.refetch and \
                not any(self.db.window_status(inst, w) for w in windows):
            windows = plan_windows(covered, opts.history_start, opts.cutoff, opts.window_days,
                                   opts.overlap_days)
        for w in windows:
            if self.db.window_status(inst, w) == "failed" and not opts.retry_failed:
                res.windows["skipped_failed"] += 1
                continue
            outcome = self._download_window(inst, w, opts, stats, res)
            res.windows[outcome] += 1
            stats["attempted"] += 1
            stats[outcome] += 1
            if outcome == "retriable":
                break  # API trouble: leave the rest of this instrument for the next run
        res.seconds = time.monotonic() - t0
        log.info("[%d/%d] %s (%s): %d window(s) planned %s; +%s rows%s in %.1fs", n, total, inst.symbol,
                 inst.security_id, len(windows), dict(res.windows) or "{}", f"{res.rows_inserted:,}",
                 f", {res.rows_revised} REVISED" if res.rows_revised else "", res.seconds)
        return res

    def _download_window(self, inst: Instrument, w: Window, opts: RunOptions, stats: Counter,
                         res: InstrumentResult) -> str:
        payload = self.tf.payload(inst, w.start, w.end, intraday_date_format=opts.intraday_date_format,
                                  client_id=opts.client_id)
        try:
            resp = self.client.post(self.tf.endpoint, payload)
        except DhanAuthError as exc:
            raise RunAborted(f"authentication rejected ({exc.message}); refresh the access token "
                             "and rerun the same command to resume") from exc
        except DhanPermanentError as exc:
            self._consecutive_retriable = 0
            if exc.error_code and exc.error_code in opts.empty_error_codes:
                empty = empty_result(self.tf)
                self.db.commit_window(inst, w, candles=empty.candles, anomalies=empty.anomalies,
                                      zero_volume=0, attempts=exc.attempts, http_status=exc.http_status,
                                      empty_error_code=exc.error_code, empty_error_message=exc.message[:500])
                return "empty"
            self.db.record_failure(inst, w, status="failed", attempts=exc.attempts,
                                   http_status=exc.http_status, error_code=exc.error_code,
                                   error_message=exc.message)
            log.error("%s %s [%s, %s): FAILED %s", inst.symbol, self.tf.name, w.start, w.end, exc.message)
            return "failed"
        except DhanTransientError as exc:
            self.db.record_failure(inst, w, status="retriable", attempts=exc.attempts,
                                   http_status=exc.http_status, error_code=exc.error_code,
                                   error_message=exc.message)
            log.error("%s %s [%s, %s): RETRIABLE after %d attempts: %s", inst.symbol, self.tf.name,
                      w.start, w.end, exc.attempts, exc.message)
            self._consecutive_retriable += 1
            if self._consecutive_retriable >= opts.breaker:
                raise RunAborted(f"{opts.breaker} consecutive windows failed with temporary errors "
                                 f"(last: {exc.message}); rerun later to resume") from exc
            return "retriable"
        self._consecutive_retriable = 0
        try:
            v = validate_response(self.tf, resp.data, w)
        except ValidationError as exc:
            self.db.record_failure(inst, w, status="failed", attempts=resp.attempts,
                                   http_status=resp.http_status, error_code=exc.code,
                                   error_message=exc.message)
            log.error("%s %s [%s, %s): FAILED validation %s", inst.symbol, self.tf.name, w.start, w.end, exc)
            return "failed"
        out = self.db.commit_window(inst, w, candles=v.candles, anomalies=v.anomalies,
                                    zero_volume=v.zero_volume_rows, attempts=resp.attempts,
                                    http_status=resp.http_status)
        res.rows_inserted += out["inserted"]
        res.rows_revised += out["revised"]
        stats["rows_inserted"] += out["inserted"]
        stats["rows_revised"] += out["revised"]
        return "complete" if v.n else "empty"
