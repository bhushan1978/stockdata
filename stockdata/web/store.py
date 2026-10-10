"""The application-control database (SQLite, WAL): the durable job queue, run history, events, campaigns, audit log,
saved symbol groups and cached catalog numbers. It is separate from the candle databases and from the request
ledger, which this module never opens."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 3                                            # 3: provider/lane/yield columns, provider checks, automation runs

ACTIVE = ("preparing", "running", "stopping")                      # a worker process may exist
WAITING = ("queued",)
RESUMABLE = ("paused", "interrupted", "stopped", "stopped_by_limit", "failed")
FINISHED_OK = ("completed", "completed_with_warnings")
TERMINAL = FINISHED_OK + ("cancelled",)
ALL_STATUSES = ACTIVE + WAITING + RESUMABLE + TERMINAL

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT NOT NULL,
    continue_on_warnings INTEGER NOT NULL DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, timeframe TEXT,
    status TEXT NOT NULL, options TEXT NOT NULL, plan TEXT, command TEXT,
    campaign_id INTEGER REFERENCES campaigns(id), campaign_pos INTEGER, queue_pos REAL NOT NULL,
    created_at TEXT NOT NULL, queued_at TEXT, started_at TEXT, finished_at TEXT, last_activity_at TEXT,
    runs INTEGER NOT NULL DEFAULT 0, exit_code INTEGER, engine_status TEXT, note TEXT, last_error TEXT,
    next_action TEXT, pending_action TEXT, block_reason TEXT,
    windows_planned INTEGER, windows_done INTEGER NOT NULL DEFAULT 0,
    instruments_total INTEGER, instruments_done INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0, attempts_used INTEGER NOT NULL DEFAULT 0,
    failed_windows INTEGER NOT NULL DEFAULT 0, retriable_windows INTEGER NOT NULL DEFAULT 0,
    progress TEXT, fingerprint TEXT, source_job_id INTEGER, not_before TEXT,
    provider TEXT, lane TEXT, yielded_to INTEGER);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(id), seq INTEGER NOT NULL,
    started_at TEXT NOT NULL, ended_at TEXT, pid INTEGER, pid_create_time REAL, control_dir TEXT NOT NULL,
    log_path TEXT NOT NULL, argv TEXT, exit_code INTEGER, engine_status TEXT, outcome TEXT, attempts_used INTEGER);
CREATE INDEX IF NOT EXISTS job_runs_job ON job_runs(job_id);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, ts TEXT NOT NULL, level TEXT NOT NULL,
    type TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT NOT NULL, subject TEXT, details TEXT);
CREATE TABLE IF NOT EXISTS symbol_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, security_ids TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, fingerprint TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL,
    ok INTEGER NOT NULL, state TEXT, message TEXT, details TEXT);
CREATE INDEX IF NOT EXISTS provider_checks_p ON provider_checks(provider, kind, id);
CREATE TABLE IF NOT EXISTS automation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL, exit_code INTEGER, summary TEXT, jobs TEXT, pid INTEGER);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._con = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.executescript(_DDL)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an older control database forward (columns are only ever added)."""
        cols = {r["name"] for r in self.all("PRAGMA table_info(jobs)")}
        if "not_before" not in cols:                              # v1 -> v2: scheduled start
            self.execute("ALTER TABLE jobs ADD COLUMN not_before TEXT")
        for col, ddl in (("provider", "TEXT"), ("lane", "TEXT"), ("yielded_to", "INTEGER")):    # v2 -> v3: workflows and providers
            if col not in cols:
                self.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # -- low level ---------------------------------------------------------------------------------------
    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._con.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable = ()) -> dict | None:
        with self._lock:
            r = self._con.execute(sql, tuple(params)).fetchone()
            return dict(r) if r else None

    def all(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, tuple(params)).fetchall()]

    def transaction(self):
        return _Tx(self)

    # -- meta / kv -----------------------------------------------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        if r is None:
            return default
        try:
            return json.loads(r["value"])
        except ValueError:
            return r["value"]

    def set_meta(self, key: str, value: Any, *, only_if_missing: bool = False) -> None:
        clause = "DO NOTHING" if only_if_missing else "DO UPDATE SET value = excluded.value"
        self.execute(f"INSERT INTO meta VALUES (?, ?) ON CONFLICT (key) {clause}", (key, json.dumps(value)))

    # -- events / audit --------------------------------------------------------------------------------------
    def event(self, job_id: int | None, type_: str, message: str, level: str = "info") -> None:
        from stockdata.web.redact import redact
        self.execute("INSERT INTO job_events (job_id, ts, level, type, message) VALUES (?, ?, ?, ?, ?)",
                     (job_id, now(), level, type_, redact(message)[:2000]))

    def audit(self, action: str, subject: str | None = None, details: dict | None = None) -> None:
        from stockdata.web.redact import redact_obj
        self.execute("INSERT INTO audit_log (ts, action, subject, details) VALUES (?, ?, ?, ?)",
                     (now(), action, subject, json.dumps(redact_obj(details or {}))))

    # -- cache -----------------------------------------------------------------------------------------------
    def cache_get(self, key: str) -> dict | None:
        r = self.one("SELECT * FROM cache WHERE key = ?", (key,))
        if not r:
            return None
        return {"value": json.loads(r["value"]), "fingerprint": r["fingerprint"], "updated_at": r["updated_at"]}

    def cache_put(self, key: str, value: Any, fingerprint: str | None = None) -> None:
        self.execute("INSERT INTO cache VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                     "fingerprint = excluded.fingerprint, updated_at = excluded.updated_at",
                     (key, json.dumps(value, default=str), fingerprint, now()))


class _Tx:
    def __init__(self, store: Store):
        self.store = store

    def __enter__(self):
        self.store._lock.acquire()
        self.store._con.execute("BEGIN IMMEDIATE")
        return self.store

    def __exit__(self, exc_type, *_):
        try:
            self.store._con.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store._lock.release()
