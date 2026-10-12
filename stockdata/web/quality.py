"""Data-quality workspace built on the repository's own validators (``stockdata.reports``).

Findings are separated into: confirmed FAILURES (an integrity check failed, or download windows are failed/retriable),
WARNINGS (odd data stored as received) and EXPECTED market-session behaviour (special sessions; a last 15-minute bar
starting at 15:00 on F&O stocks after 3 August 2026). Nothing is synthesised and nothing is adjusted.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from stockdata import reconcile
from stockdata.db import CandleDB
from stockdata.reports import coverage, integrity_checks
from stockdata.timeframes import get_timeframe
from stockdata.web.data import DataAccess, DatabaseBusy, DatabaseMissing
from stockdata.web.store import Store

ANOMALY_NOTES = {
    "weekend_candle": ("expected", "A Saturday/Sunday session (e.g. the Budget-day special session). Real data, stored as received."),
    "outside_session": ("expected", "A bar outside 09:15-15:30 (e.g. the Muhurat evening session). Real data, stored as received."),
    "post_close_bar": ("expected", "A bar starting at 15:30 (present in some periods). Real data, stored as received."),
    "off_grid": ("warning", "A bar that is not on the 15-minute grid. Stored as received; worth a look."),
    "negative_volume": ("warning", "A bar with negative volume as sent by Dhan. Stored as received, never corrected."),
    "volume_rounded": ("warning", "Dhan sent a volume with a fractional part (shares are whole numbers; this usually comes from an adjustment applied by the "
                                  "provider). It was stored rounded to the nearest whole share, which changed the response's total volume by under 0.1%; "
                                  "the value as received is in the flag detail."),
    "duplicate_dropped": ("warning", "Dhan sent the identical candle more than once in one response. One copy was stored; nothing was lost."),
    "duplicate_conflict": ("warning", "Dhan sent two or more DIFFERENT candles for one date or time. None was stored (a visible gap) and nothing was "
                                      "merged or guessed; the raw values of each are in the flag detail so you can decide."),
}
FNO_NOTE = ("From 3 August 2026 the last 15-minute bar of a session starts at 15:00 for F&O stocks. That is expected and is not "
            "reported as a Dhan data defect.")


class QualityRunner:
    """Runs the (potentially long) checks in a background thread, holding a gate reader so no writer starts under it."""

    def __init__(self, data: DataAccess, store: Store):
        self.data, self.store = data, store
        self._lock = threading.Lock()
        self._running: dict[str, dict] = {}
        self._cons: dict[str, object] = {}

    def state(self, tf: str) -> dict:
        cached = self.store.cache_get(f"quality:{tf}")
        run = self._running.get(tf)
        return {"timeframe": tf, "running": bool(run and not run.get("done")), "error": run.get("error") if run else None,
                "started_at": run.get("started_at") if run else None,
                "report": cached["value"] if cached else None, "computed_at": cached["updated_at"] if cached else None,
                "stale": bool(cached and cached["fingerprint"] != self.data.fingerprint(tf)) if cached else None}

    def start(self, tf: str) -> dict:
        with self._lock:
            if self._running.get(tf) and not self._running[tf].get("done"):
                return self.state(tf)
            # fail fast with the explanatory error if the database is not readable right now
            with self.data.connect(tf):
                pass
            self._running[tf] = {"done": False, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            threading.Thread(target=self._run, args=(tf,), daemon=True, name=f"quality-{tf}").start()
        return self.state(tf)

    def cancel(self, tf: str) -> None:
        con = self._cons.get(tf)
        if con is not None:
            try:
                con.interrupt()
            except Exception:
                pass

    def _run(self, tf: str) -> None:
        run = self._running[tf]
        try:
            t = get_timeframe(tf)
            fp = self.data.fingerprint(tf)
            with self.data.gate.reader(tf):
                with CandleDB(t, self.data.path(tf), read_only=True) as db:
                    self._cons[tf] = db.con
                    report = build_report(db)
            self.store.cache_put(f"quality:{tf}", report, fp)
        except (DatabaseBusy, DatabaseMissing) as exc:
            run["error"] = str(exc)
        except Exception as exc:                       # noqa: BLE001 - surfaced to the UI, not swallowed
            run["error"] = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            self._cons.pop(tf, None)
            run["done"] = True


def build_report(db: CandleDB) -> dict:
    tf = db.tf
    checks = integrity_checks(db)
    cov = coverage(db)
    failures = [{"name": c.name, "detail": c.detail} for c in checks if not c.ok]
    passed = [{"name": c.name, "detail": c.detail} for c in checks if c.ok]
    outstanding = db.outstanding_failures()
    out_rows = [{"security_id": o[1], "from": str(o[2])[:10], "to": str(o[3])[:10], "status": o[4], "attempts": o[5],
                 "error_code": o[6], "message": (o[7] or "")[:200]} for o in outstanding[:500]]
    for col in ("candles", "outstanding_failed", "outstanding_retriable", "missing_market_days", "zero_volume", "revisions", "anomalies"):
        if col in cov:
            cov[col] = cov[col].fillna(0)             # instruments with no candles yet have NA counts
    if "is_active" in cov:
        cov["is_active"] = cov["is_active"].fillna(False).astype(bool)
    sym = dict(zip(cov["security_id"], cov["symbol"]))
    for r in out_rows:
        r["symbol"] = sym.get(r["security_id"])
    an = db.con.execute("SELECT kind, count(*), count(DISTINCT security_id), CAST(min(candle_key) AS VARCHAR), "
                        "CAST(max(candle_key) AS VARCHAR) FROM data_anomalies GROUP BY 1 ORDER BY 2 DESC").fetchall()
    anomalies = []
    for kind, n, insts, lo, hi in an:
        cls, note = ANOMALY_NOTES.get(kind, ("warning", "Flagged on ingest; stored as received."))
        anomalies.append({"kind": kind, "count": n, "instruments": insts, "first": lo, "last": hi, "classification": cls, "note": note})
    attention = []
    active = cov[cov["is_active"]] if "is_active" in cov else cov
    for r in active.itertuples():
        reasons = []
        if getattr(r, "outstanding_failed", 0) > 0:
            reasons.append(f"{r.outstanding_failed} failed window(s)")
        if getattr(r, "outstanding_retriable", 0) > 0:
            reasons.append(f"{r.outstanding_retriable} retriable window(s)")
        if not r.candles > 0:
            reasons.append("no candles stored")
        if not tf.is_intraday and getattr(r, "missing_market_days", 0) > 0:
            reasons.append(f"{int(r.missing_market_days)} market day(s) missing inside its range")
        if reasons:
            attention.append({"symbol": r.symbol, "security_id": int(r.security_id), "reasons": reasons})
    totals = {"instruments": int(len(cov)), "with_candles": int((cov["candles"] > 0).sum()),
              "candles": int(cov["candles"].sum()), "instruments_with_zero_volume_bars": int((cov["zero_volume"] > 0).sum()),
              "revisions": int(cov["revisions"].sum())}
    extra = {}
    if tf.is_intraday:
        dist = db.con.execute(f"""SELECT n, count(*) FROM (SELECT security_id, CAST({tf.key_col} AS DATE) d, count(*) n
                                  FROM {tf.table} GROUP BY 1, 2) GROUP BY 1 ORDER BY 2 DESC LIMIT 8""").fetchall()
        extra["bars_per_session"] = [{"bars": b, "sessions": c} for b, c in dist]
        extra["expected_note"] = FNO_NOTE
    return {"timeframe": tf.name, "computed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "totals": totals,
            "integrity": {"failures": failures, "passed": passed},
            "outstanding_windows": out_rows, "outstanding_total": len(outstanding),
            "anomalies": anomalies, "attention": attention[:1000], "attention_total": len(attention),
            "reconciliation": reconcile.build(db),
            "coverage_columns": list(cov.columns), **extra,
            "severity_summary": {"confirmed_failures": len(failures) + (1 if outstanding else 0),
                                 "warnings": sum(a["count"] for a in anomalies if a["classification"] == "warning"),
                                 "expected": sum(a["count"] for a in anomalies if a["classification"] == "expected")}}


def lifecycle_report(data: DataAccess, tf: str) -> dict:
    """Instrument identity over time (renames, reused symbols, shared ISINs, new listings, instruments that left the master)."""
    t = get_timeframe(tf)
    if not data.path(tf).exists():
        raise DatabaseMissing(f"The {tf} database does not exist yet.")
    with data.gate.reader(tf):
        try:
            with CandleDB(t, data.path(tf), read_only=True) as db:
                return reconcile.lifecycle(db)
        except Exception as exc:
            from stockdata.web.data import _is_lock_error
            if _is_lock_error(exc):
                raise DatabaseBusy(tf, external=True) from exc
            raise


def coverage_csv(data: DataAccess, tf: str):
    """Fresh per-instrument coverage as a DataFrame (read-only), for download."""
    t = get_timeframe(tf)
    if not data.path(tf).exists():
        raise DatabaseMissing(f"The {tf} database does not exist yet.")
    with data.gate.reader(tf):
        try:
            with CandleDB(t, data.path(tf), read_only=True) as db:
                return coverage(db)
        except Exception as exc:
            from stockdata.web.data import _is_lock_error
            if _is_lock_error(exc):
                raise DatabaseBusy(tf, external=True) from exc
            raise
