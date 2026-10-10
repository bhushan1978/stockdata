"""Read access to the candle databases for the browser: catalog numbers, instrument coverage, candle queries.

DuckDB allows exactly one writer process per file and no readers while it writes. So every read goes through
``DbGate``: while a download worker owns a timeframe's database the gate refuses the read (``DatabaseBusy``), and
catalog numbers come from the last cached snapshot, clearly labelled with its age. Reads open a short-lived
read-only connection and close it immediately; the scheduler waits for open readers before it starts a worker.
All SQL is parameterised; table and column names come from fixed whitelists only.
"""
from __future__ import annotations

import csv
import io
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

import duckdb
import numpy as np

from stockdata import corp_actions as ca

from stockdata.timeframes import TIMEFRAMES, get_timeframe
from stockdata.web.store import Store

BASES = ("raw", "adjusted")
ADJUSTED_LABEL = "SPLIT/BONUS ADJUSTED (verified events only; prices and volume; no dividend adjustment)"
RAW_LABEL = "RAW (exactly as stored from the provider)"
TIMEZONE_NOTE = ("IST (Asia/Kolkata). Candle times are stored as naive IST wall-clock values; chart axes show "
                 "those wall-clock values unchanged.")
MAX_BARS = 150_000
TABLE_COLUMNS = {"time": None, "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume",
                 "epoch_raw": "epoch_raw", "ingested": "ingested_at_utc", "window_id": "window_id"}


class DatabaseBusy(Exception):
    def __init__(self, timeframe: str, job_id: int | None = None, external: bool = False, maintenance: str | None = None):
        self.timeframe, self.job_id, self.external, self.maintenance = timeframe, job_id, external, maintenance
        if maintenance:
            super().__init__(f"The {timeframe} database is being worked on by maintenance ({maintenance}). Data views for this "
                             "timeframe are unavailable for a short while and no download can start on it until it finishes.")
            return
        who = (f"download job #{job_id}" if job_id else "another program") if not external else "another program"
        super().__init__(f"The {timeframe} database is being written by {who}. DuckDB allows one writer at a time, so "
                         "data views for this timeframe are unavailable until it is paused or finished. "
                         "Cached catalog numbers are shown where available.")


class DatabaseMissing(Exception):
    pass


class DbGate:
    """Admits short read-only connections only while no process writes the file, and tells the scheduler when a database
    is reserved by maintenance (backup / log replay) so it cannot start a writer under it."""

    def __init__(self, writer_lookup: Callable[[str], int | None]):
        self._lookup = writer_lookup
        self._cond = threading.Condition()
        self._readers = {tf: 0 for tf in TIMEFRAMES}
        self._claims: dict[str, int] = {tf: 0 for tf in TIMEFRAMES}
        self._maintenance: dict[str, str] = {}

    def writer(self, tf: str) -> int | None:
        return self._lookup(tf)

    def maintenance(self, tf: str) -> str | None:
        return self._maintenance.get(tf)

    @contextmanager
    def reader(self, tf: str):
        with self._cond:
            if tf in self._maintenance:
                raise DatabaseBusy(tf, maintenance=self._maintenance[tf])
            job = self._lookup(tf)
            if job is not None or self._claims[tf] > 0:
                raise DatabaseBusy(tf, job)
            self._readers[tf] += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers[tf] -= 1
                self._cond.notify_all()

    def claim_for_writer(self, tf: str, timeout: float = 10.0) -> bool:
        """Scheduler: stop admitting readers and wait for the open ones to finish."""
        with self._cond:
            if tf in self._maintenance:
                return False
            self._claims[tf] += 1
            if self._cond.wait_for(lambda: self._readers[tf] == 0, timeout):
                return True
            self._claims[tf] -= 1
            return False

    def release_claim(self, tf: str) -> None:
        with self._cond:
            self._claims[tf] = max(0, self._claims[tf] - 1)
            self._cond.notify_all()

    def begin_maintenance(self, tf: str, label: str, timeout: float = 10.0) -> bool:
        """Reserve the database for a maintenance action: no new readers, no writer start. False if it cannot be reserved."""
        with self._cond:
            if tf in self._maintenance or self._lookup(tf) is not None:
                return False
            self._maintenance[tf] = label
            if self._cond.wait_for(lambda: self._readers[tf] == 0, timeout):
                return True
            del self._maintenance[tf]
            return False

    def end_maintenance(self, tf: str) -> None:
        with self._cond:
            self._maintenance.pop(tf, None)
            self._cond.notify_all()

    def readers(self, tf: str) -> int:
        return self._readers[tf]


def _is_lock_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(s in text for s in ("could not set lock", "conflicting lock", "being used by another process",
                                   "cannot acquire", "resource temporarily unavailable"))


class DataAccess:
    def __init__(self, data_dir: Path, store: Store, gate: DbGate):
        self.data_dir, self.store, self.gate = Path(data_dir), store, gate
        self._compute_locks: dict[str, threading.Lock] = {}
        self._compute_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._compute_guard:
            return self._compute_locks.setdefault(key, threading.Lock())

    # -- files -----------------------------------------------------------------------------------------------
    def path(self, tf: str) -> Path:
        return self.data_dir / get_timeframe(tf).db_name

    def file_info(self, tf: str) -> dict:
        p = self.path(tf)
        wal = p.with_name(p.name + ".wal")
        info = {"path": str(p), "exists": p.exists(), "size_bytes": 0, "wal_bytes": 0, "modified": None}
        if p.exists():
            st = p.stat()
            info.update(size_bytes=st.st_size, modified=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"))
        if wal.exists():
            info["wal_bytes"] = wal.stat().st_size
        return info

    def fingerprint(self, tf: str) -> str:
        i = self.file_info(tf)
        p = self.path(tf)
        mt = int(p.stat().st_mtime_ns) if p.exists() else 0
        return f"{i['size_bytes']}:{i['wal_bytes']}:{mt}"

    @contextmanager
    def connect(self, tf: str, *, hold: bool = True):
        """Short-lived READ-ONLY connection, admitted by the gate."""
        if not self.path(tf).exists():
            raise DatabaseMissing(f"The {tf} database does not exist yet ({self.path(tf)}). Load the instrument master first.")
        with self.gate.reader(tf):
            try:
                con = duckdb.connect(str(self.path(tf)), read_only=True)
            except duckdb.Error as exc:
                if _is_lock_error(exc):
                    raise DatabaseBusy(tf, external=True) from exc
                raise
            try:
                yield con
            finally:
                con.close()

    # -- cached catalog numbers ---------------------------------------------------------------------------------
    def _cached(self, key: str, tf: str, compute: Callable[[duckdb.DuckDBPyConnection], dict], *, force: bool = False) -> dict:
        """Live when the database is readable (recomputed only if the file changed), else the cached snapshot."""
        fp = self.fingerprint(tf)
        cached = self.store.cache_get(key)
        writer = self.gate.writer(tf)
        if cached and cached["fingerprint"] == fp and not force:
            return {**cached["value"], "source": "cache-current", "cached_at": cached["updated_at"], "stale": False,
                    "writer_job_id": writer}
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            # another request is already scanning this database for the same numbers (a big scan can take many seconds
            # and the interface polls): serve the last snapshot meanwhile, or wait if there is none yet
            if cached:
                return {**cached["value"], "source": "cache", "cached_at": cached["updated_at"], "stale": True,
                        "stale_reason": "refreshing in the background", "writer_job_id": writer}
            lock.acquire()
            lock.release()
            return self._cached(key, tf, compute, force=False)
        try:
            with self.connect(tf) as con:
                value = compute(con)
            self.store.cache_put(key, value, fp)
            return {**value, "source": "live", "cached_at": None, "stale": False, "writer_job_id": writer}
        except DatabaseBusy as busy:
            if cached:
                return {**cached["value"], "source": "cache", "cached_at": cached["updated_at"], "stale": True,
                        "stale_reason": str(busy), "writer_job_id": writer}
            return {"source": "unavailable", "stale": True, "stale_reason": str(busy), "writer_job_id": writer}
        except DatabaseMissing as missing:
            return {"source": "unavailable", "stale": False, "stale_reason": str(missing), "missing": True}
        finally:
            lock.release()

    def summary(self, tf: str, *, force: bool = False) -> dict:
        t = get_timeframe(tf)

        def compute(con):
            n, lo, hi, inst = con.execute(
                f"SELECT count(*), min({t.key_col}), max({t.key_col}), count(DISTINCT security_id) FROM {t.table}").fetchone()
            universe = con.execute("SELECT count(*), count(*) FILTER (WHERE is_active) FROM instruments").fetchone()
            failures = con.execute("SELECT count(*) FROM download_windows WHERE status IN ('failed','retriable')").fetchone()[0]
            wins = dict(con.execute("SELECT status, count(*) FROM download_windows GROUP BY 1").fetchall())
            last_run = con.execute("SELECT run_id, finished_at_utc, status FROM download_runs "
                                   "WHERE finished_at_utc IS NOT NULL ORDER BY run_id DESC LIMIT 1").fetchone()
            last_ok = con.execute("SELECT max(finished_at_utc) FROM download_runs "
                                  "WHERE status IN ('completed', 'completed_with_errors')").fetchone()[0]
            load = con.execute("SELECT loaded_at_utc, source, rows_total, rows_in_universe, universe "
                               "FROM instrument_master_loads ORDER BY load_id DESC LIMIT 1").fetchone()
            return {"candles": n, "first": str(lo) if lo is not None else None, "last": str(hi) if hi is not None else None,
                    "instruments_with_data": inst, "instruments_total": universe[0], "instruments_active": universe[1],
                    "windows": wins, "failed_windows_recorded": failures,
                    "last_run": {"run_id": last_run[0], "finished_at_utc": str(last_run[1]), "status": last_run[2]} if last_run else None,
                    "last_successful_run_utc": str(last_ok) if last_ok else None,
                    "master_load": {"loaded_at_utc": str(load[0]), "source": load[1], "rows_total": load[2],
                                    "rows_in_universe": load[3], "universe": load[4]} if load else None}

        return {"timeframe": tf, "file": self.file_info(tf), **self._cached(f"summary:{tf}", tf, compute, force=force)}

    def instrument_coverage(self, tf: str, *, force: bool = False) -> dict:
        t = get_timeframe(tf)

        def compute(con):
            rows = con.execute(
                f"""SELECT i.security_id, i.symbol, i.name, i.exchange_segment, i.instrument, i.series, i.is_active,
                           s.candles, CAST(s.first AS VARCHAR), CAST(s.last AS VARCHAR),
                           coalesce(f.failed, 0), coalesce(f.retriable, 0)
                    FROM instruments i
                    LEFT JOIN (SELECT exchange_segment, security_id, count(*) AS candles, min({t.key_col}) AS first,
                                      max({t.key_col}) AS last FROM {t.table} GROUP BY 1, 2) s
                      USING (exchange_segment, security_id)
                    LEFT JOIN (SELECT exchange_segment, security_id,
                                      count(*) FILTER (WHERE status = 'failed') AS failed,
                                      count(*) FILTER (WHERE status = 'retriable') AS retriable
                               FROM download_windows w WHERE status IN ('failed','retriable') AND NOT EXISTS (
                                 SELECT 1 FROM download_windows c WHERE c.exchange_segment = w.exchange_segment
                                 AND c.security_id = w.security_id AND c.status IN ('complete','empty')
                                 AND c.window_from <= w.window_from AND c.window_to >= w.window_to)
                               GROUP BY 1, 2) f USING (exchange_segment, security_id)
                    ORDER BY i.symbol""").fetchall()
            keys = ("security_id", "symbol", "name", "exchange_segment", "instrument", "series", "is_active",
                    "candles", "first", "last", "failed_windows", "retriable_windows")
            return {"rows": [dict(zip(keys, r)) for r in rows]}

        return self._cached(f"coverage:{tf}", tf, compute, force=force)

    def instrument_master(self) -> dict:
        """The loaded universe (identical in both databases), from whichever database is readable."""
        for tf in ("daily", "15min"):
            if not self.path(tf).exists():
                continue
            def compute(con):
                rows = con.execute("SELECT security_id, symbol, name, exchange_segment, instrument, series, is_active "
                                   "FROM instruments ORDER BY symbol").fetchall()
                keys = ("security_id", "symbol", "name", "exchange_segment", "instrument", "series", "is_active")
                return {"rows": [dict(zip(keys, r)) for r in rows]}
            res = self._cached(f"master:{tf}", tf, compute)
            if res.get("rows") is not None:
                return res
        return {"rows": [], "source": "unavailable", "stale": False, "stale_reason": "No database exists yet. Load the instrument master first."}

    def resolve_instrument(self, security_id: int) -> dict | None:
        for r in self.instrument_master().get("rows", []):
            if r["security_id"] == security_id:
                return r
        return None

    def search(self, q: str, limit: int = 15) -> list[dict]:
        q = q.strip().upper()
        rows = self.instrument_master().get("rows", [])
        if not q:
            return rows[:limit]
        starts = [r for r in rows if (r["symbol"] or "").upper().startswith(q)]
        contains = [r for r in rows if q in (r["symbol"] or "").upper() and r not in starts]
        names = [r for r in rows if q in (r["name"] or "").upper() and r not in starts and r not in contains]
        return (starts + contains + names)[:limit]

    def ensure_schema(self, busy: Callable[[str], bool]) -> list[str]:
        """Bring a database written by an older version up to date (additive columns only), when nothing is writing it.
        Returns the databases that were migrated. Anything busy or locked is left alone: the worker that owns it migrates it
        when it opens it for writing."""
        done = []
        for tf in TIMEFRAMES:
            p = self.path(tf)
            if not p.exists() or busy(tf):
                continue
            try:
                from stockdata.db import CandleDB
                con = duckdb.connect(str(p), read_only=True)
                try:
                    have = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                                                      [get_timeframe(tf).table]).fetchall()}
                finally:
                    con.close()
                if "provider" in have:
                    continue
                with CandleDB(get_timeframe(tf), p):
                    done.append(tf)
            except Exception:                                    # noqa: BLE001 - locked by another program: not our business here
                continue
        return done

    # -- candles ------------------------------------------------------------------------------------------------
    def _bounds(self, tf: str, start: date | None, end: date | None) -> tuple[str, list]:
        t = get_timeframe(tf)
        key = t.key_col
        where, params = ["exchange_segment = ?", "security_id = ?"], []
        if start:
            where.append(f"{key} >= ?"); params.append(datetime.combine(start, datetime.min.time()) if t.is_intraday else start)
        if end:   # inclusive end date in the UI -> exclusive next midnight here
            nxt = end + timedelta(days=1)
            where.append(f"{key} < ?"); params.append(datetime.combine(nxt, datetime.min.time()) if t.is_intraday else nxt)
        return " AND ".join(where), params

    @staticmethod
    def _adjust_rows(rows: list, events: list[tuple[date, float, float]]) -> tuple[list, list[float]]:
        """rows: (epoch, o, h, l, c, v) with the epoch of the naive IST wall-clock time. Returns the adjusted rows and the
        cumulative price factor of each (a candle is multiplied by every event whose ex-date is after its own date)."""
        if not rows or not events:
            return rows, [1.0] * len(rows)
        days = np.array([datetime.fromtimestamp(r[0], tz=timezone.utc).date() for r in rows])
        pf, vf = np.ones(len(rows)), np.ones(len(rows))
        for ex, p, v in events:
            mask = days < ex
            pf[mask] *= p
            vf[mask] *= v
        out = [(r[0], r[1] * pf[i], r[2] * pf[i], r[3] * pf[i], r[4] * pf[i], r[5] * vf[i]) for i, r in enumerate(rows)]
        return out, [float(x) for x in pf]

    def _events_for_chart(self, con, seg: str, sec: int) -> list[dict]:
        try:
            rows = con.execute("""SELECT action_id, event_type, ex_date, ratio_text, price_factor, verification_status, apply_adjustment,
                                  application_status FROM corporate_actions WHERE exchange_segment = ? AND security_id = ? ORDER BY ex_date""",
                               [seg, sec]).fetchall()
        except duckdb.Error:
            return []
        return [{"action_id": r[0], "type": r[1], "ex_date": r[2].isoformat(), "ratio": r[3], "factor": r[4], "status": r[5],
                 "adjusts": bool(r[6]) and r[5] == "verified", "application_status": r[7]} for r in rows]

    def candles(self, tf: str, security_id: int, *, start: date | None = None, end: date | None = None,
                bars: int | None = None, basis: str = "raw", as_of: date | None = None) -> dict:
        if basis not in BASES:
            raise ValueError(f"basis must be one of {', '.join(BASES)}")
        t = get_timeframe(tf)
        inst = self.resolve_instrument(security_id)
        if inst is None:
            raise KeyError(f"security id {security_id} is not in the loaded instrument universe")
        seg = inst["exchange_segment"]
        key = t.key_col
        epoch_expr = f"CAST(epoch(CAST({key} AS TIMESTAMP)) AS BIGINT)"
        default_bars = 750 if tf == "daily" else 2500
        limit = min(bars or default_bars, MAX_BARS) if not (start or end) else MAX_BARS + 1
        where, params = self._bounds(tf, start, end)
        with self.connect(tf) as con:
            if start or end:
                rows = con.execute(f"SELECT {epoch_expr}, open, high, low, close, volume FROM {t.table} WHERE {where} "
                                   f"ORDER BY {key} LIMIT ?", [seg, security_id, *params, limit]).fetchall()
            else:
                rows = con.execute(f"SELECT {epoch_expr}, open, high, low, close, volume FROM {t.table} WHERE {where} "
                                   f"ORDER BY {key} DESC LIMIT ?", [seg, security_id, *params, limit]).fetchall()[::-1]
            truncated = len(rows) > MAX_BARS
            rows = rows[:MAX_BARS]
            flags = []
            if rows:
                lo = datetime.utcfromtimestamp(rows[0][0]); hi = datetime.utcfromtimestamp(rows[-1][0])
                flags = con.execute(
                    f"SELECT {'CAST(epoch(candle_key) AS BIGINT)'}, kind FROM data_anomalies WHERE exchange_segment = ? "
                    "AND security_id = ? AND candle_key >= ? AND candle_key <= ? ORDER BY candle_key LIMIT 5000",
                    [seg, security_id, lo, hi]).fetchall()
            coverage = self._coverage(con, t, seg, security_id)
            warnings = self._warnings(con, t, seg, security_id, rows, coverage, start, end)
            chart_events = self._events_for_chart(con, seg, security_id)
            adjustment = {"basis": basis, "label": RAW_LABEL, "version": None, "as_of": None, "events_applied": [], "status": "raw"}
            if basis == "adjusted":
                events = ca.verified_events(con, seg, security_id, as_of=as_of)
                rows, _ = self._adjust_rows(rows, events)
                state = ca.adjustment_summary(con)
                adjustment = {"basis": "adjusted", "label": ADJUSTED_LABEL + (f", as known on {as_of} (no look-ahead)" if as_of else ""),
                              "version": state["version"], "as_of": as_of.isoformat() if as_of else None,
                              "events_applied": [{"ex_date": e[0].isoformat(), "price_factor": e[1], "volume_factor": e[2]} for e in events],
                              "status": "adjusted" if events else "no_verified_events", "note": state["basis_note"]}
                if not events:
                    adjustment["label"] += ": NO verified event applies to this instrument, so these values equal the raw ones"
        cols = list(zip(*rows)) if rows else [[]] * 6
        return {"timeframe": tf, "security_id": security_id, "symbol": inst["symbol"], "name": inst["name"],
                "timezone": TIMEZONE_NOTE, "n": len(rows), "truncated": truncated,
                "basis": basis, "adjustment": adjustment, "corporate_actions": chart_events,
                "t": list(cols[0]), "o": list(cols[1]), "h": list(cols[2]), "l": list(cols[3]), "c": list(cols[4]),
                "v": list(cols[5]), "flags": [{"t": f[0], "kind": f[1]} for f in flags],
                "coverage": coverage, "warnings": warnings}

    def _coverage(self, con, t, seg: str, sec: int) -> dict:
        key = t.key_col
        n, lo, hi = con.execute(f"SELECT count(*), CAST(min({key}) AS VARCHAR), CAST(max({key}) AS VARCHAR) FROM {t.table} "
                                "WHERE exchange_segment = ? AND security_id = ?", [seg, sec]).fetchone()
        w = con.execute("SELECT CAST(min(window_from) FILTER (WHERE status IN ('complete','empty')) AS VARCHAR), "
                        "CAST(max(window_to) FILTER (WHERE status IN ('complete','empty')) AS VARCHAR), "
                        "count(*) FILTER (WHERE status='complete'), count(*) FILTER (WHERE status='empty') "
                        "FROM download_windows WHERE exchange_segment = ? AND security_id = ?", [seg, sec]).fetchone()
        fails = con.execute(
            """SELECT CAST(window_from AS VARCHAR), CAST(window_to AS VARCHAR), status, error_code, error_message, attempts
               FROM download_windows w WHERE exchange_segment = ? AND security_id = ? AND status IN ('failed','retriable')
               AND NOT EXISTS (SELECT 1 FROM download_windows c WHERE c.exchange_segment = w.exchange_segment
                 AND c.security_id = w.security_id AND c.status IN ('complete','empty')
                 AND c.window_from <= w.window_from AND c.window_to >= w.window_to) ORDER BY window_from""",
            [seg, sec]).fetchall()
        return {"bars_total": n, "first_candle": lo, "last_candle": hi, "covered_from": w[0], "covered_to": w[1],
                "windows_complete": w[2], "windows_empty": w[3],
                "outstanding": [{"from": f[0], "to": f[1], "status": f[2], "error_code": f[3],
                                 "message": (f[4] or "")[:300], "attempts": f[5]} for f in fails]}

    def _warnings(self, con, t, seg, sec, rows, coverage, start, end) -> list[dict]:
        out: list[dict] = []
        key = t.key_col
        if coverage["bars_total"] == 0:
            out.append({"level": "warning", "type": "no_data", "message": "No candles are stored for this instrument yet."})
            return out
        if coverage["outstanding"]:
            out.append({"level": "failure", "type": "failed_windows",
                        "message": f"{len(coverage['outstanding'])} failed/retriable download window(s) are outstanding for this instrument."})
        cf, ct = coverage["covered_from"], coverage["covered_to"]
        if start and cf and str(start) < cf[:10]:
            out.append({"level": "info", "type": "not_downloaded_yet",
                        "message": f"The selected range starts before the downloaded coverage ({cf[:10]}); earlier history is not downloaded."})
        if end and ct and str(end) >= ct[:10]:
            out.append({"level": "info", "type": "not_downloaded_yet",
                        "message": f"The selected range reaches beyond the downloaded coverage (up to, not including, {ct[:10]})."})
        if rows:
            lo = datetime.utcfromtimestamp(rows[0][0]); hi = datetime.utcfromtimestamp(rows[-1][0])
            gap = con.execute(
                f"""SELECT CAST(prev AS VARCHAR), CAST(cur AS VARCHAR), date_diff('day', CAST(prev AS DATE), CAST(cur AS DATE))
                    FROM (SELECT {key} AS cur, lag({key}) OVER (ORDER BY {key}) AS prev FROM {t.table}
                          WHERE exchange_segment = ? AND security_id = ? AND {key} >= ? AND {key} <= ?)
                    WHERE prev IS NOT NULL AND date_diff('day', CAST(prev AS DATE), CAST(cur AS DATE)) > 6
                    ORDER BY cur LIMIT 20""", [seg, sec, lo if t.is_intraday else lo.date(), hi if t.is_intraday else hi.date()]).fetchall()
            for prev, cur, days in gap:
                out.append({"level": "warning", "type": "gap",
                            "message": f"{days}-day calendar gap with no candle between {prev[:10]} and {cur[:10]} (market holidays can explain short gaps; missing candles are never filled in)."})
            if t.is_intraday:
                part = con.execute(
                    f"""SELECT count(*) FROM (SELECT CAST({key} AS DATE) d, count(*) n FROM {t.table}
                        WHERE exchange_segment = ? AND security_id = ? AND {key} >= ? AND {key} <= ?
                        GROUP BY 1 HAVING count(*) < 20)""", [seg, sec, lo, hi]).fetchone()[0]
                if part:
                    out.append({"level": "info", "type": "partial_sessions",
                                "message": f"{part} session(s) in this range have fewer than 20 bars (illiquid names skip empty bars; "
                                           "special or shortened sessions are normal). A last bar starting at 15:00 is expected "
                                           "for F&O stocks after 3 August 2026 and is not a defect."})
        fl = con.execute("SELECT kind, count(*) FROM data_anomalies WHERE exchange_segment = ? AND security_id = ? GROUP BY 1",
                         [seg, sec]).fetchall()
        if fl:
            out.append({"level": "info", "type": "flagged",
                        "message": "Flagged on ingest (stored as received): " + ", ".join(f"{k} x{n}" for k, n in fl)})
        return out

    # -- raw table -------------------------------------------------------------------------------------------------
    def table(self, tf: str, security_id: int, *, start: date | None, end: date | None, sort: str = "time",
              desc: bool = True, page: int = 1, page_size: int = 100, basis: str = "raw", as_of: date | None = None) -> dict:
        if basis not in BASES:
            raise ValueError(f"basis must be one of {', '.join(BASES)}")
        t = get_timeframe(tf)
        inst = self.resolve_instrument(security_id)
        if inst is None:
            raise KeyError(f"security id {security_id} is not in the loaded instrument universe")
        if sort not in TABLE_COLUMNS:
            raise ValueError(f"cannot sort by {sort!r}")
        col = TABLE_COLUMNS[sort] or t.key_col
        if basis == "adjusted" and sort in ("open", "high", "low", "close", "volume"):
            raise ValueError("sorting by a price or volume column is only available on the raw basis (adjusted values are derived per row)")
        page_size = max(1, min(page_size, 1000))
        page = max(1, page)
        where, params = self._bounds(tf, start, end)
        key = t.key_col
        with self.connect(tf) as con:
            total = con.execute(f"SELECT count(*) FROM {t.table} WHERE {where}", [inst["exchange_segment"], security_id, *params]).fetchone()[0]
            rows = con.execute(
                f"""SELECT CAST({key} AS VARCHAR), open, high, low, close, volume, epoch_raw, window_id,
                           CAST(ingested_at_utc AS VARCHAR),
                           (SELECT string_agg(a.kind, ',') FROM data_anomalies a WHERE a.exchange_segment = c.exchange_segment
                            AND a.security_id = c.security_id AND a.candle_key = CAST(c.{key} AS TIMESTAMP)),
                           coalesce(c.provider, 'dhan'),
                           (SELECT count(*) FROM candle_revisions r WHERE r.exchange_segment = c.exchange_segment
                            AND r.security_id = c.security_id AND r.candle_key = CAST(c.{key} AS TIMESTAMP)),
                           CAST(epoch(CAST({key} AS TIMESTAMP)) AS BIGINT)
                    FROM {t.table} c WHERE {where} ORDER BY {col} {'DESC' if desc else 'ASC'}, {key} LIMIT ? OFFSET ?""",
                [inst["exchange_segment"], security_id, *params, page_size, (page - 1) * page_size]).fetchall()
            events = ca.verified_events(con, inst["exchange_segment"], security_id, as_of=as_of) if basis == "adjusted" else []
        keys = ("time", "open", "high", "low", "close", "volume", "epoch_raw", "window_id", "ingested_utc", "flags", "provider", "revisions")
        out_rows = []
        for r in rows:
            rec = dict(zip(keys, r[:12]))
            if basis == "adjusted" and events:
                (_, o, h, l, c, v), = self._adjust_rows([(r[12], rec["open"], rec["high"], rec["low"], rec["close"], rec["volume"])], events)[0]
                rec.update(open=o, high=h, low=l, close=c, volume=v)
            rec["basis"] = basis
            out_rows.append(rec)
        return {"timeframe": tf, "symbol": inst["symbol"], "security_id": security_id, "total": total, "page": page,
                "page_size": page_size, "timezone": TIMEZONE_NOTE, "basis": basis,
                "label": RAW_LABEL if basis == "raw" else ADJUSTED_LABEL + (f", as known on {as_of}" if as_of else ""),
                "rows": out_rows}

    def export_csv(self, tf: str, security_id: int, *, start: date | None, end: date | None,
                   max_rows: int = 2_000_000, basis: str = "raw", as_of: date | None = None) -> Iterator[bytes]:
        """Streaming CSV of the stored candles: exactly as stored (``basis=raw``, no synthesis, no adjustment) or split/bonus
        adjusted from verified events only (``basis=adjusted``, optionally as known on ``as_of``). Every row says which."""
        if basis not in BASES:
            raise ValueError(f"basis must be one of {', '.join(BASES)}")
        t = get_timeframe(tf)
        inst = self.resolve_instrument(security_id)
        if inst is None:
            raise KeyError(f"security id {security_id} is not in the loaded instrument universe")
        where, params = self._bounds(tf, start, end)
        key = t.key_col
        with self.connect(tf) as con:
            events, version = [], 0
            if basis == "adjusted":
                events = ca.verified_events(con, inst["exchange_segment"], security_id, as_of=as_of)
                version = ca.current_version(con)
            label = "raw" if basis == "raw" else (f"adjusted_v{version}" + (f"_as_of_{as_of}" if as_of else "") + ("" if events else "_no_events"))
            cur = con.execute(f"SELECT CAST({key} AS VARCHAR), open, high, low, close, volume, epoch_raw, coalesce(provider, 'dhan'), "
                              f"CAST(epoch(CAST({key} AS TIMESTAMP)) AS BIGINT) FROM {t.table} "
                              f"WHERE {where} ORDER BY {key} LIMIT ?", [inst["exchange_segment"], security_id, *params, max_rows])
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["symbol", "security_id", "timeframe", f"{key}_ist", "open", "high", "low", "close", "volume", "epoch_raw", "provider", "basis"])
            yield buf.getvalue().encode(); buf.seek(0); buf.truncate()
            while True:
                chunk = cur.fetchmany(20_000)
                if not chunk:
                    break
                if events:
                    adj, _ = self._adjust_rows([(r[8], r[1], r[2], r[3], r[4], r[5]) for r in chunk], events)
                    chunk = [(r[0], a[1], a[2], a[3], a[4], a[5], r[6], r[7], r[8]) for r, a in zip(chunk, adj)]
                for r in chunk:
                    w.writerow([inst["symbol"], security_id, tf, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], label])
                yield buf.getvalue().encode(); buf.seek(0); buf.truncate()
