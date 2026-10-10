"""Database maintenance actions that are safe to offer from the interface.

* **Recover (replay the write-ahead log)** - after a crash, kill or power loss a database can carry a write-ahead log. Opening
  the file for writing once replays it and a CHECKPOINT folds it into the main file. Nothing is deleted or changed in the data.
* **Back up** - copies the (checkpointed) database file to ``<data dir>/backups`` after checking free space. Restoring is a
  deliberate manual step (copy the file back while the application is stopped) and is not offered as a button.

Both reserve the database through the gate first: while one runs, reads are refused with an explanation and the scheduler will
not start a writer on that database. Neither can run while a download job owns the database.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from stockdata.db import CandleDB
from stockdata.timeframes import TIMEFRAMES, get_timeframe
from stockdata.web.data import DataAccess, DatabaseMissing
from stockdata.web.process import free_bytes
from stockdata.web.store import Store
from stockdata.web.supervisor import JobError

BACKUP_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,120}\.duckdb$")
MIN_HEADROOM = 512 * 1024 ** 2


class Maintenance:
    def __init__(self, data_dir: Path, data: DataAccess, store: Store):
        self.dir = Path(data_dir) / "backups"
        self.data, self.store = data, store
        self.state: dict[str, dict] = {tf: {"running": False} for tf in TIMEFRAMES}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- status
    def backups(self) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.glob("*.duckdb"), key=lambda q: q.stat().st_mtime, reverse=True):
            st = p.stat()
            out.append({"name": p.name, "bytes": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                        "timeframe": "daily" if "_daily_" in p.name else "15min" if "_15min_" in p.name else None})
        return out

    def status(self) -> dict:
        return {"directory": str(self.dir), "backups": self.backups(), "tasks": self.state,
                "free_bytes": free_bytes(self.dir if self.dir.exists() else self.data.data_dir),
                "restore_note": "To restore, stop the application, copy a backup over the database file (keep the .wal file out of the way), then start it again."}

    # ---------------------------------------------------------------- recover
    def recover(self, tf: str) -> dict:
        t = get_timeframe(tf)
        path = self.data.path(tf)
        if not path.exists():
            raise DatabaseMissing(f"The {tf} database does not exist yet ({path}).")
        with self._lock:
            if self.state[tf].get("running"):
                raise JobError(f"A maintenance action is already running on the {tf} database.", 409)
        if not self.data.gate.begin_maintenance(tf, "log replay"):
            raise JobError(f"The {tf} database is in use (a download job owns it, or a data view is still open). Pause the job first.", 409)
        started = time.time()
        before = self.data.file_info(tf)
        try:
            with CandleDB(t, path) as db:                      # opening for writing replays the write-ahead log
                db.con.execute("CHECKPOINT")
                rows = db.con.execute(f"SELECT count(*) FROM {t.table}").fetchone()[0]
        finally:
            self.data.gate.end_maintenance(tf)
        after = self.data.file_info(tf)
        result = {"timeframe": tf, "candles": rows, "wal_bytes_before": before["wal_bytes"], "wal_bytes_after": after["wal_bytes"],
                  "size_before": before["size_bytes"], "size_after": after["size_bytes"], "seconds": round(time.time() - started, 1),
                  "message": ("The write-ahead log was replayed and folded into the database file." if before["wal_bytes"]
                              else "There was no pending write-ahead log; the database was opened, checkpointed and closed cleanly.")}
        self.store.audit("maintenance.recover", tf, result)
        return result

    # ---------------------------------------------------------------- backup
    def start_backup(self, tf: str) -> dict:
        path = self.data.path(tf)
        if not path.exists():
            raise DatabaseMissing(f"The {tf} database does not exist yet ({path}).")
        size = path.stat().st_size
        self.dir.mkdir(parents=True, exist_ok=True)
        free = free_bytes(self.dir)
        if free < size * 1.05 + MIN_HEADROOM:
            raise JobError(f"Not enough free disk space for a backup: the database is {size / 1e9:.2f} GB and {free / 1e9:.2f} GB is free "
                           "(a backup needs the size plus some headroom).", 409)
        with self._lock:
            if self.state[tf].get("running"):
                raise JobError(f"A maintenance action is already running on the {tf} database.", 409)
            if not self.data.gate.begin_maintenance(tf, "backup"):
                raise JobError(f"The {tf} database is in use (a download job owns it, or a data view is still open). Pause the job first.", 409)
            self.state[tf] = {"running": True, "kind": "backup", "started_at": datetime.now().isoformat(timespec="seconds"),
                              "bytes_total": size, "bytes_copied": 0, "error": None, "file": None}
        threading.Thread(target=self._backup, args=(tf,), daemon=True, name=f"backup-{tf}").start()
        return self.state[tf]

    def _backup(self, tf: str) -> None:
        st = self.state[tf]
        t = get_timeframe(tf)
        src = self.data.path(tf)
        dest = self.dir / f"{Path(t.db_name).stem}_{datetime.now():%Y%m%d_%H%M%S}.duckdb"
        tmp = dest.with_suffix(".part")
        try:
            with CandleDB(t, src) as db:                       # fold any log into the file so the copy is self-contained
                db.con.execute("CHECKPOINT")
            total = src.stat().st_size
            st["bytes_total"] = total
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    chunk = fin.read(8 * 1024 ** 2)
                    if not chunk:
                        break
                    fout.write(chunk)
                    st["bytes_copied"] += len(chunk)
            if tmp.stat().st_size != total:
                raise OSError("the copy has a different size than the database; it was discarded")
            tmp.replace(dest)
            st["file"] = dest.name
            self.store.audit("maintenance.backup", tf, {"file": dest.name, "bytes": total})
        except Exception as exc:                                # noqa: BLE001 - reported to the user
            st["error"] = f"{type(exc).__name__}: {exc}"[:400]
            try:
                tmp.unlink()
            except OSError:
                pass
        finally:
            self.data.gate.end_maintenance(tf)
            st["running"] = False
            st["finished_at"] = datetime.now().isoformat(timespec="seconds")

    def delete_backup(self, name: str) -> None:
        if not BACKUP_NAME.match(name):
            raise JobError("invalid backup name", 400)
        p = self.dir / name
        if not p.is_file():
            raise JobError("that backup does not exist", 404)
        p.unlink()
        self.store.audit("maintenance.delete_backup", name, {})
