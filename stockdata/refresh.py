"""Explicit, auditable refresh of stored candles for chosen symbols, one timeframe and a date range.

Why: Dhan revises history retroactively (e.g. split/bonus adjustment), and ``download --refetch`` only *records*
differences. A refresh replaces the stored candles with Dhan's current data, safely:

* the replacement window is fetched and fully validated BEFORE any stored candle is touched;
* the window is applied in ONE transaction (audit rows + candle changes + job state), or not at all;
* authentication failures, server errors, request-limit stops, validation failures and EMPTY responses never
  change a candle. An empty response while candles exist is held for review, never treated as "delete";
* candles Dhan no longer returns are KEPT unless ``prune_missing`` is set, and are archived before removal;
* every change is recorded: old versus new in ``candle_revisions`` and in ``refresh_changes`` (with the job id);
* progress is kept per job/window, so an interrupted refresh resumes where it stopped. Repeating a *completed*
  refresh starts a new job and is a no-op on the candles when Dhan's data is unchanged.

Data is preserved exactly as received: no corporate-action factor is applied here. Each timeframe has its own
database and its own jobs, so a daily refresh can never touch the 15-minute database or vice versa.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from stockdata.budget import RequestLimitReached
from stockdata.db import CandleDB
from stockdata.dhan import DhanAuthError, DhanClient, DhanPermanentError, DhanTransientError
from stockdata.downloader import RunAborted, limit_abort
from stockdata.instruments import Instrument
from stockdata.timeutil import utcnow
from stockdata.validation import ValidationError, validate_response
from stockdata.windows import Window, chunk_backward

log = logging.getLogger("stockdata.refresh")

RESOLVED = ("unchanged", "replaced", "empty", "held_empty")
OLD_COLS = ("open", "high", "low", "close", "volume")


@dataclass
class RefreshOptions:
    start: datetime
    end: datetime                       # exclusive
    window_days: int
    prune_missing: bool = False         # delete (after archiving) stored candles absent from a validated, non-empty response
    retry_failed: bool = False
    intraday_date_format: str = "datetime"
    client_id: str | None = None
    breaker: int = 5


@dataclass
class InstrumentRefresh:
    symbol: str
    security_id: int
    job_id: int | None = None
    resumed: bool = False
    windows: Counter = field(default_factory=Counter)
    changed: int = 0
    added: int = 0
    missing: int = 0
    removed: int = 0
    seconds: float = 0.0


class Refresher:
    def __init__(self, db: CandleDB, client: DhanClient):
        self.db, self.client, self.tf = db, client, db.tf
        self._consecutive_retriable = 0

    # ------------------------------------------------------------------ run
    def run(self, instruments: list[Instrument], opts: RefreshOptions, *, command: str = "") -> dict:
        if opts.start >= opts.end:
            raise ValueError("refresh range is empty: start must be before the exclusive end")
        if self.tf.max_window_days and opts.window_days > self.tf.max_window_days:
            raise ValueError(f"window of {opts.window_days} days exceeds the {self.tf.max_window_days}-day cap")
        stats: Counter = Counter(instruments=len(instruments))
        results: list[InstrumentRefresh] = []
        run_id = self.db.start_run(command or f"refresh {self.tf.name}")
        status, note = "failed", ""
        sent_before = self.client.requests_sent
        self._consecutive_retriable = 0
        try:
            for n, inst in enumerate(instruments, 1):
                results.append(self._refresh_instrument(n, len(instruments), inst, opts, stats))
            bad = stats["failed"] or stats["retriable"] or stats["held_empty"] or \
                any(r.missing and not opts.prune_missing for r in results)
            status = "completed_with_issues" if bad else "completed"
        except RunAborted as exc:
            status, note = exc.status, str(exc)
            log.error("refresh %s: %s", "stopped at a request limit" if exc.status == "stopped_request_limit" else "aborted", exc)
        except KeyboardInterrupt:
            status, note = "aborted", "interrupted by user"
            log.warning("interrupted; the refresh job is checkpointed, rerun the same command to resume")
            raise
        finally:
            stats["requests_made"] = self.client.requests_sent - sent_before
            self.db.finish_run(run_id, status, {"instruments": len(instruments), "attempted": stats["requested"],
                                                "complete": stats["replaced"] + stats["unchanged"] + stats["empty"],
                                                "failed": stats["failed"], "retriable": stats["retriable"],
                                                "rows_inserted": stats["added"]}, note)
        stats["status"], stats["note"], stats["results"] = status, note, results
        return stats

    # ------------------------------------------------------------------ jobs
    def plan(self, inst: Instrument, opts: RefreshOptions) -> tuple[int | None, list[Window]]:
        """(open job id or None, windows still to request) without writing anything."""
        con = self.db.con
        row = con.execute("""SELECT job_id FROM refresh_jobs WHERE exchange_segment = ? AND security_id = ?
                             AND range_from = ? AND range_to = ? AND status = 'open' ORDER BY job_id DESC LIMIT 1""",
                          [inst.exchange_segment, inst.security_id, opts.start, opts.end]).fetchone()
        if row:
            todo = con.execute("""SELECT window_from, window_to FROM refresh_windows WHERE job_id = ?
                                  AND status IN ('pending', 'retriable'""" + (", 'failed'" if opts.retry_failed else "") +
                               ") ORDER BY window_to DESC", [row[0]]).fetchall()
            return row[0], [Window(a, b) for a, b in todo]
        return None, chunk_backward(opts.start, opts.end, opts.window_days)

    def _open_job(self, inst: Instrument, opts: RefreshOptions, command: str) -> tuple[int, bool]:
        con = self.db.con
        job_id, todo = self.plan(inst, opts)
        if job_id is not None:
            return job_id, True
        con.begin()
        try:
            job_id = con.execute("SELECT nextval('refresh_job_seq')").fetchone()[0]
            con.execute("INSERT INTO refresh_jobs VALUES (?, ?, ?, ?, ?, ?, 'open', ?, NULL, ?, NULL)",
                        [job_id, inst.exchange_segment, inst.security_id, opts.start, opts.end, opts.prune_missing,
                         utcnow(), command or f"refresh {self.tf.name}"])
            for w in todo:
                con.execute("INSERT INTO refresh_windows (job_id, window_from, window_to, status, attempts) "
                            "VALUES (?, ?, ?, 'pending', 0)", [job_id, w.start, w.end])
            con.commit()
        except Exception:
            con.rollback()
            raise
        return job_id, False

    def _finish_job(self, job_id: int) -> None:
        con = self.db.con
        open_n = con.execute("SELECT count(*) FROM refresh_windows WHERE job_id = ? AND status NOT IN "
                             "('unchanged', 'replaced', 'empty', 'held_empty')", [job_id]).fetchone()[0]
        if open_n:
            return
        held, missing = con.execute("SELECT count(*) FILTER (WHERE status = 'held_empty'), coalesce(sum(rows_missing), 0) "
                                    "FROM refresh_windows WHERE job_id = ?", [job_id]).fetchone()
        note = "; ".join(x for x in (f"{held} empty response(s) held for review, no candle deleted" if held else "",
                                     f"{missing} stored candle(s) absent from Dhan's data" if missing else "") if x)
        con.execute("UPDATE refresh_jobs SET status = 'complete', finished_at_utc = ?, note = ? WHERE job_id = ?",
                    [utcnow(), note or None, job_id])

    # ---------------------------------------------------------------- per instrument
    def _refresh_instrument(self, n: int, total: int, inst: Instrument, opts: RefreshOptions,
                            stats: Counter) -> InstrumentRefresh:
        t0 = time.monotonic()
        res = InstrumentRefresh(inst.symbol, inst.security_id)
        res.job_id, res.resumed = self._open_job(inst, opts, f"refresh {self.tf.name}")
        prune = bool(self.db.con.execute("SELECT prune_missing FROM refresh_jobs WHERE job_id = ?", [res.job_id]).fetchone()[0])
        _, todo = self.plan(inst, opts)
        for w in todo:
            outcome = self._refresh_window(res.job_id, inst, w, opts, prune, res)
            res.windows[outcome] += 1
            stats[outcome] += 1
            stats["requested"] += 1
            if outcome == "retriable":
                break                                    # API trouble: leave the rest of this job for the next run
        self._finish_job(res.job_id)
        stats["added"] += res.added
        res.seconds = time.monotonic() - t0
        log.info("[%d/%d] %s (%s) job %d%s: %s; changed %d, added %d, missing-from-source %d%s in %.1fs", n, total,
                 inst.symbol, inst.security_id, res.job_id, " (resumed)" if res.resumed else "", dict(res.windows) or "{}",
                 res.changed, res.added, res.missing, f", removed {res.removed}" if res.removed else "", res.seconds)
        return res

    # ----------------------------------------------------------------- per window
    def _refresh_window(self, job_id: int, inst: Instrument, w: Window, opts: RefreshOptions, prune: bool,
                        res: InstrumentRefresh) -> str:
        payload = self.tf.payload(inst, w.start, w.end, intraday_date_format=opts.intraday_date_format,
                                  client_id=opts.client_id)
        try:
            resp = self.client.post(self.tf.endpoint, payload)
        except RequestLimitReached as exc:
            raise limit_abort(exc) from exc              # nothing recorded, nothing changed
        except DhanAuthError as exc:
            raise RunAborted(f"authentication rejected ({exc.message}); refresh the access token and rerun the same "
                             "command to resume. No stored candle was changed.") from exc
        except DhanPermanentError as exc:                # includes the ambiguous daily DH-907: never read as "empty"
            self._mark(job_id, w, "failed", exc.attempts, exc.http_status, exc.error_code, exc.message)
            self._consecutive_retriable = 0
            log.error("%s %s refresh [%s, %s): FAILED %s (stored candles untouched)", inst.symbol, self.tf.name, w.start, w.end, exc.message)
            return "failed"
        except DhanTransientError as exc:
            self._mark(job_id, w, "retriable", exc.attempts, exc.http_status, exc.error_code, exc.message)
            self._consecutive_retriable += 1
            log.error("%s %s refresh [%s, %s): RETRIABLE after %d attempts: %s (stored candles untouched)",
                      inst.symbol, self.tf.name, w.start, w.end, exc.attempts, exc.message)
            if self._consecutive_retriable >= opts.breaker:
                raise RunAborted(f"{opts.breaker} consecutive windows failed with temporary errors (last: {exc.message}); "
                                 "rerun later to resume") from exc
            return "retriable"
        self._consecutive_retriable = 0
        try:
            v = validate_response(self.tf, resp.data, w)  # validate fully BEFORE touching any stored candle
        except ValidationError as exc:
            self._mark(job_id, w, "failed", resp.attempts, resp.http_status, exc.code, exc.message)
            log.error("%s %s refresh [%s, %s): FAILED validation %s (stored candles untouched)", inst.symbol, self.tf.name, w.start, w.end, exc)
            return "failed"
        return self._apply(job_id, inst, w, v, resp.attempts, resp.http_status, prune, res)

    def _mark(self, job_id: int, w: Window, status: str, attempts: int, http_status, error_code, error_message) -> None:
        """Record a non-applied outcome. Never touches candles."""
        now = utcnow()
        self.db.con.execute(
            """UPDATE refresh_windows SET status = ?, attempts = attempts + ?, http_status = ?, error_code = ?,
               error_message = ?, first_attempt_at_utc = coalesce(first_attempt_at_utc, ?), last_attempt_at_utc = ?
               WHERE job_id = ? AND window_from = ? AND window_to = ?""",
            [status, attempts, http_status, error_code, (error_message or "")[:500], now, now, job_id, w.start, w.end])

    # --------------------------------------------------------------- the atomic apply
    def _apply(self, job_id: int, inst: Instrument, w: Window, v, attempts: int, http_status, prune: bool,
               res: InstrumentRefresh) -> str:
        db, tf, con = self.db, self.tf, self.db.con
        t, k, now = tf.table, tf.key_col, utcnow()
        key_s = f"CAST(s.{k} AS DATE)" if not tf.is_intraday else f"s.{k}"
        span = (f"c.{k} >= CAST(? AS DATE) AND c.{k} < CAST(? AS DATE)" if not tf.is_intraday
                else f"c.{k} >= ? AND c.{k} < ?")
        span_plain = span.replace("c.", "")
        ident = [inst.exchange_segment, inst.security_id]
        rng = [w.start, w.end]
        join = f"c.exchange_segment = s.exchange_segment AND c.security_id = s.security_id AND c.{k} = {key_s}"
        differs_for = lambda alias: " OR ".join(f"{alias}.{x} IS DISTINCT FROM s.{x}" for x in OLD_COLS)
        differs = differs_for("c")
        stored = f"c.exchange_segment = ? AND c.security_id = ? AND {span}"
        olds = ", ".join(f"c.{x}" for x in OLD_COLS)
        news = ", ".join(f"s.{x}" for x in OLD_COLS)
        nulls = ", ".join("NULL" for _ in OLD_COLS)
        con.begin()
        try:
            old_n = con.execute(f"SELECT count(*) FROM {t} c WHERE {stored}", ident + rng).fetchone()[0]
            if v.n == 0:
                # An empty response is ambiguous: it may mean "no data" or "temporarily unavailable". Never delete.
                status = "held_empty" if old_n else "empty"
                self._set(job_id, w, status, attempts, http_status, old_n, 0, 0, 0, 0, 0, now)
                con.commit()
                if old_n:
                    log.warning("%s %s refresh [%s, %s): empty response while %d candles are stored; HELD for review, nothing deleted",
                                inst.symbol, tf.name, w.start, w.end, old_n)
                return status
            stage = v.candles.copy()
            stage.insert(0, "security_id", inst.security_id)
            stage.insert(0, "exchange_segment", inst.exchange_segment)
            con.register("rstage", stage)
            an = v.anomalies.copy()
            an.insert(0, "security_id", inst.security_id)
            an.insert(0, "exchange_segment", inst.exchange_segment)
            con.register("ran", an)
            try:
                changed = con.execute(f"SELECT count(*) FROM {t} c JOIN rstage s ON {join} WHERE {differs}").fetchone()[0]
                added = con.execute(f"SELECT count(*) FROM rstage s WHERE NOT EXISTS (SELECT 1 FROM {t} c WHERE {join})").fetchone()[0]
                gone = f"{stored} AND NOT EXISTS (SELECT 1 FROM rstage s WHERE {join})"
                missing = con.execute(f"SELECT count(*) FROM {t} c WHERE {gone}", ident + rng).fetchone()[0]
                # ---- audit first (same transaction), old versus new
                if changed:
                    con.execute(f"""INSERT INTO refresh_changes SELECT ?, c.exchange_segment, c.security_id,
                        CAST(c.{k} AS TIMESTAMP), 'revised', {olds}, {news}, ? FROM {t} c JOIN rstage s ON {join}
                        WHERE {differs}""", [job_id, now])
                    con.execute(f"""INSERT INTO candle_revisions SELECT c.exchange_segment, c.security_id,
                        CAST(c.{k} AS TIMESTAMP), {olds}, {news}, c.window_id, ? FROM {t} c JOIN rstage s ON {join}
                        WHERE {differs} ON CONFLICT DO NOTHING""", [now])
                if added:
                    con.execute(f"""INSERT INTO refresh_changes SELECT ?, s.exchange_segment, s.security_id,
                        CAST(s.{k} AS TIMESTAMP), 'added', {nulls}, {news}, ? FROM rstage s
                        WHERE NOT EXISTS (SELECT 1 FROM {t} c WHERE {join})""", [job_id, now])
                if missing:
                    con.execute(f"""INSERT INTO refresh_changes SELECT ?, c.exchange_segment, c.security_id,
                        CAST(c.{k} AS TIMESTAMP), ?, {olds}, {nulls}, ? FROM {t} c WHERE {gone}""",
                                [job_id, "removed" if prune else "missing_kept", now] + ident + rng)
                # ---- then the changes themselves
                if changed:
                    con.execute(f"""UPDATE {t} SET open = s.open, high = s.high, low = s.low, close = s.close,
                        volume = s.volume, epoch_raw = s.epoch_raw, ingested_at_utc = ? FROM rstage s
                        WHERE {t}.exchange_segment = s.exchange_segment AND {t}.security_id = s.security_id
                        AND {t}.{k} = {key_s} AND ({differs_for(t)})""", [now])
                if added:
                    wid = db._window_id(inst, w)
                    con.execute(f"""INSERT INTO {t} (exchange_segment, security_id, {k}, open, high, low, close, volume,
                        epoch_raw, window_id, ingested_at_utc)
                        SELECT s.exchange_segment, s.security_id, {key_s}, s.open, s.high, s.low, s.close, s.volume,
                               s.epoch_raw, ?, ? FROM rstage s WHERE NOT EXISTS (SELECT 1 FROM {t} c WHERE {join})
                        ON CONFLICT DO NOTHING""", [wid, now])
                    db._upsert_window(wid, inst, w, status="complete", attempts=attempts, received=v.n, inserted=added,
                                      flagged=int(v.anomalies["candle_key"].nunique()), revised=changed,
                                      zero_volume=v.zero_volume_rows, http_status=http_status, error_code=None,
                                      error_message=None, now=now)
                removed = 0
                if prune and missing:
                    con.execute(f"""DELETE FROM {t} WHERE exchange_segment = ? AND security_id = ? AND {span_plain}
                        AND NOT EXISTS (SELECT 1 FROM rstage s WHERE {t}.exchange_segment = s.exchange_segment
                        AND {t}.security_id = s.security_id AND {t}.{k} = {key_s})""", ident + rng)
                    removed = missing
                if len(an):
                    con.execute(f"""INSERT INTO data_anomalies SELECT a.exchange_segment, a.security_id, a.candle_key,
                        a.kind, a.detail, c.window_id, ? FROM ran a JOIN {t} c ON c.exchange_segment = a.exchange_segment
                        AND c.security_id = a.security_id AND CAST(c.{k} AS TIMESTAMP) = a.candle_key
                        ON CONFLICT DO NOTHING""", [now])
            finally:
                con.unregister("rstage")
                con.unregister("ran")
            status = "replaced" if (changed or added or removed) else "unchanged"
            self._set(job_id, w, status, attempts, http_status, old_n, v.n, changed, added, missing, removed, now)
            con.commit()
        except Exception:
            con.rollback()
            raise
        res.changed += changed
        res.added += added
        res.missing += missing
        res.removed += removed
        return status

    def _set(self, job_id, w, status, attempts, http_status, old_n, new_n, changed, added, missing, removed, now) -> None:
        self.db.con.execute(
            """UPDATE refresh_windows SET status = ?, attempts = attempts + ?, rows_old = ?, rows_new = ?, rows_changed = ?,
               rows_added = ?, rows_missing = ?, rows_removed = ?, http_status = ?, error_code = NULL, error_message = NULL,
               first_attempt_at_utc = coalesce(first_attempt_at_utc, ?), last_attempt_at_utc = ?, applied_at_utc = ?
               WHERE job_id = ? AND window_from = ? AND window_to = ?""",
            [status, attempts, old_n, new_n, changed, added, missing, removed, http_status, now, now, now, job_id, w.start, w.end])
