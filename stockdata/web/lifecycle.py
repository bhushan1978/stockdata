"""One server per data directory: an OS-level lock held for the server's lifetime, and a small info file the stop
script reads. The lock is released by the OS if the server crashes, so a crash never leaves a stale 'running' state."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


class ServerLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def write_info(path: Path, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields.update(pid=os.getpid(), started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields))
    os.replace(tmp, path)


def read_info(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def remove_info(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass
