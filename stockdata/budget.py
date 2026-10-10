"""Request safety limits.

* a per-run cap (default 60,000 outbound attempts), and
* a per-IST-calendar-day cap (default 80,000) shared by every process and by both the daily and the
  15-minute downloaders through a small persistent ledger (SQLite: safe for concurrent processes).

Every outbound API attempt is counted *before* it is sent, retries included, so a crash can only
over-count, never under-count. The ledger deliberately lives outside the candle databases so those
stay completely independent of each other.

The ledger only knows about THIS application. Other programs using the same Dhan account are not
counted, while Dhan enforces its documented 100,000 requests/day on the whole account.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from stockdata.timeutil import IST, utcnow

log = logging.getLogger("stockdata.budget")

DHAN_DOCUMENTED_DAILY_LIMIT = 100_000
DEFAULT_MAX_PER_RUN = 60_000
DEFAULT_MAX_PER_DAY = 80_000
WARN_FRACTION = 0.9

COUNTER_CAVEAT = ("This counter only covers requests made by this application. Other applications using the same Dhan "
                  f"account are NOT included, and Dhan enforces {DHAN_DOCUMENTED_DAILY_LIMIT:,} requests/day account-wide.")


class RequestLimitReached(Exception):
    """A safety cap was hit. Raised BEFORE the request is sent; nothing was recorded for the window."""

    def __init__(self, kind: str, limit: int, used: int, day: str):
        self.kind, self.limit, self.used, self.day = kind, limit, used, day
        what = "per-run" if kind == "run" else f"per-day (IST {day})"
        super().__init__(f"{what} request limit reached ({used:,} used, limit {limit:,})")


def validate_limits(per_run: int, per_day: int) -> None:
    if per_run < 1 or per_day < 1:
        raise ValueError("request limits must be positive integers")
    if per_day > DHAN_DOCUMENTED_DAILY_LIMIT:
        raise ValueError(f"the per-day limit {per_day:,} exceeds Dhan's documented {DHAN_DOCUMENTED_DAILY_LIMIT:,} "
                         "requests/day; refusing to configure it")


def ist_day(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(IST).date().isoformat()


class UsageLedger:
    """Persistent per-IST-day request counters, atomic across processes."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(IST))
        self._con = sqlite3.connect(str(self.path), timeout=60, isolation_level=None)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.executescript("""
            CREATE TABLE IF NOT EXISTS usage (
                day TEXT NOT NULL, label TEXT NOT NULL, count INTEGER NOT NULL, last_utc TEXT NOT NULL,
                PRIMARY KEY (day, label));
            CREATE TABLE IF NOT EXISTS limit_events (
                ts_utc TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, label TEXT, used INTEGER, limit_value INTEGER);
        """)

    def close(self) -> None:
        self._con.close()

    def day(self) -> str:
        return ist_day(self._clock)

    def reserve(self, label: str, max_per_day: int) -> int:
        """Atomically count one attempt for today. Returns today's total (all labels) after counting.
        Raises RequestLimitReached WITHOUT counting if the day's total is already at the cap."""
        day, now = self.day(), utcnow().isoformat(timespec="seconds")
        con = self._con
        con.execute("BEGIN IMMEDIATE")          # takes the write lock: concurrent processes serialise here
        try:
            total = con.execute("SELECT COALESCE(SUM(count), 0) FROM usage WHERE day = ?", (day,)).fetchone()[0]
            if total >= max_per_day:
                con.execute("INSERT INTO limit_events VALUES (?, ?, 'day', ?, ?, ?)", (now, day, label, total, max_per_day))
                con.execute("COMMIT")
                raise RequestLimitReached("day", max_per_day, total, day)
            con.execute("INSERT INTO usage VALUES (?, ?, 1, ?) ON CONFLICT (day, label) DO UPDATE "
                        "SET count = count + 1, last_utc = excluded.last_utc", (day, label, now))
            con.execute("COMMIT")
            return total + 1
        except RequestLimitReached:
            raise
        except Exception:
            con.execute("ROLLBACK")
            raise

    def usage(self, day: str | None = None) -> dict[str, int]:
        rows = self._con.execute("SELECT label, count FROM usage WHERE day = ? ORDER BY label", (day or self.day(),)).fetchall()
        return {label: n for label, n in rows}

    def history(self, days: int = 7) -> list[tuple[str, int]]:
        return self._con.execute("SELECT day, SUM(count) FROM usage GROUP BY day ORDER BY day DESC LIMIT ?", (days,)).fetchall()


def read_usage(path: str | Path, day: str | None = None, *, history: int | None = None):
    """Read usage without creating or locking anything. Returns {label: count} for a day, or, with
    ``history=N``, [(day, total), ...] for the last N days. A missing ledger means no usage."""
    path = Path(path)
    if not path.exists():
        return [] if history else {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        if history:
            return con.execute("SELECT day, SUM(count) FROM usage GROUP BY day ORDER BY day DESC LIMIT ?", (history,)).fetchall()
        return {k: n for k, n in con.execute("SELECT label, count FROM usage WHERE day = ? ORDER BY label",
                                             (day or ist_day(lambda: datetime.now(IST)),)).fetchall()}
    except sqlite3.OperationalError:       # ledger exists but has no tables yet
        return [] if history else {}
    finally:
        con.close()


class RequestBudget:
    """Counts attempts for one run against the per-run cap and the shared per-day ledger."""

    def __init__(self, ledger: UsageLedger, label: str, *, max_per_run: int = DEFAULT_MAX_PER_RUN,
                 max_per_day: int = DEFAULT_MAX_PER_DAY):
        validate_limits(max_per_run, max_per_day)
        self.ledger, self.label = ledger, label
        self.max_per_run, self.max_per_day = max_per_run, max_per_day
        self.run_used = 0
        self.day_used = 0
        self._warned: set[str] = set()

    def acquire(self) -> None:
        """Call immediately before every outbound attempt (retries included)."""
        if self.run_used >= self.max_per_run:
            raise RequestLimitReached("run", self.max_per_run, self.run_used, self.ledger.day())
        self.day_used = self.ledger.reserve(self.label, self.max_per_day)
        self.run_used += 1
        self._warn_if_close("run", self.run_used, self.max_per_run)
        self._warn_if_close("day", self.day_used, self.max_per_day)

    def _warn_if_close(self, kind: str, used: int, limit: int) -> None:
        if kind not in self._warned and used >= limit * WARN_FRACTION:
            self._warned.add(kind)
            log.warning("%s request usage at %d of %d (%.0f%%)", kind, used, limit, 100 * used / limit)

    def summary(self) -> str:
        today = self.ledger.usage()
        parts = ", ".join(f"{k}={v:,}" for k, v in today.items()) or "none"
        return (f"requests this run: {self.run_used:,} of {self.max_per_run:,}; today (IST {self.ledger.day()}): "
                f"{sum(today.values()):,} of {self.max_per_day:,} ({parts}). {COUNTER_CAVEAT}")
