"""Optional run control for a download that is supervised by another program (the web application).

It is OFF unless ``STOCKDATA_CONTROL_DIR`` is set, so a normal command-line run is byte-for-byte unchanged.
When on, the downloader

* checks a *stop request file* between windows and exits cleanly after the in-flight window has been committed
  (an API call that is already on the wire is never abandoned half-way), and
* publishes a small progress file (atomically replaced) that the supervisor reads: plan size, current instrument and
  window, counters, attempts and a heartbeat. The supervisor never touches the DuckDB file a worker is writing.

Nothing here talks to the network or to a database.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from stockdata.timeutil import utcnow

ENV_VAR = "STOCKDATA_CONTROL_DIR"
STOP_FILE = "stop.request"
PROGRESS_FILE = "progress.json"
WRITE_INTERVAL = 0.5          # seconds; window completions are always written


_instances: dict[str, "JobControl"] = {}
_instances_lock = threading.Lock()


def forget_instances() -> None:
    """Tests: drop the per-process instances."""
    with _instances_lock:
        _instances.clear()


class JobControl:
    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stop_path = self.dir / STOP_FILE
        self.progress_path = self.dir / PROGRESS_FILE
        self._lock = threading.Lock()
        self._last_write = 0.0
        self.state: dict = {
            "pid": os.getpid(), "started_at": utcnow().isoformat(timespec="seconds"), "phase": "starting",
            "plan": None, "instrument": None, "window": None, "requests": 0, "last_event": None,
            "last_error": None,
            "counts": {"windows_done": 0, "complete": 0, "empty": 0, "failed": 0, "retriable": 0,
                       "skipped_failed": 0, "rows_inserted": 0, "rows_revised": 0, "instruments_done": 0, "retries": 0},
        }

    @classmethod
    def from_env(cls) -> "JobControl | None":
        """One instance per process and directory: the supervising wrapper and the engine share their state, so
        neither overwrites the other's progress snapshot."""
        value = os.environ.get(ENV_VAR)
        if not value:
            return None
        key = str(Path(value))
        with _instances_lock:
            if key not in _instances:
                _instances[key] = cls(value)
            return _instances[key]

    # -- stop requests ---------------------------------------------------------
    def should_stop(self) -> bool:
        return self.stop_path.exists()

    def stop_reason(self) -> str:
        try:
            return self.stop_path.read_text().strip() or "stop"
        except OSError:
            return "stop"

    def request_stop(self, reason: str = "stop") -> None:
        """Used by the supervisor and by the worker's own signal handler."""
        self.stop_path.write_text(reason)

    # -- progress ----------------------------------------------------------------
    def report(self, event: str, **fields) -> None:
        """Record an event. Event names: plan, instrument_start, window_start, window_done, instrument_done,
        phase (fields: phase=...), error (fields: message=...)."""
        with self._lock:
            st, counts = self.state, self.state["counts"]
            st["last_event"] = event
            if event == "plan":
                st["plan"] = {"instruments": fields["instruments"], "windows": fields["windows"]}
            elif event == "instrument_start":
                st["instrument"] = {"index": fields["index"], "total": fields["total"],
                                    "symbol": fields["symbol"], "security_id": fields["security_id"]}
                st["window"] = None
            elif event == "window_start":
                st["window"] = {"from": fields["start"], "to": fields["end"]}
            elif event == "window_done":
                outcome = fields["outcome"]
                counts["windows_done"] += 1
                counts[outcome] = counts.get(outcome, 0) + 1
                counts["rows_inserted"] += fields.get("rows_inserted", 0)
                counts["rows_revised"] += fields.get("rows_revised", 0)
                counts["retries"] += max(0, fields.get("attempts", 1) - 1)      # attempts beyond the first, per window
                if outcome in ("failed", "retriable") and fields.get("message"):
                    st["last_error"] = fields["message"][:300]
                st["window"] = None
            elif event == "instrument_done":
                counts["instruments_done"] += 1
                counts["skipped_failed"] += fields.get("skipped_failed", 0)
            elif event == "phase":
                st["phase"] = fields["phase"]
            elif event == "error":
                st["last_error"] = str(fields.get("message", ""))[:300]
            if "requests" in fields:
                st["requests"] = fields["requests"]
            important = event in ("plan", "window_done", "instrument_done", "phase", "error")
            self._flush(force=important)

    def heartbeat(self) -> None:
        with self._lock:
            self._flush(force=True)

    def _flush(self, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < WRITE_INTERVAL:
            return
        self._last_write = now
        self.state["updated_at"] = utcnow().isoformat(timespec="seconds")
        data = json.dumps(self.state)
        tmp = self.progress_path.with_suffix(".tmp")
        try:
            tmp.write_text(data)
            for attempt in range(5):      # Windows: replace can fail while a reader has the file open
                try:
                    os.replace(tmp, self.progress_path)
                    return
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
        except OSError:
            pass                          # progress is advisory; never fail a download because of it


def read_progress(directory: str | os.PathLike) -> dict | None:
    """Supervisor side: the last complete progress snapshot, or None."""
    try:
        return json.loads((Path(directory) / PROGRESS_FILE).read_text())
    except (OSError, ValueError):
        return None
