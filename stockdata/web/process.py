"""Cross-platform process helpers for detached download workers.

Workers must outlive a server restart (closing the browser or the console must not kill a multi-hour download),
so they are started detached: ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` on Windows (not attached to the
server's console, immune to its Ctrl+C / window close) and a new session on POSIX. A process is identified by
(pid, creation time) so a recycled pid is never mistaken for a worker."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

IS_WINDOWS = sys.platform == "win32"


def spawn_detached(argv: list[str], *, cwd: Path, env: dict, log_path: Path) -> tuple[subprocess.Popen, float]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logfile = open(log_path, "ab", buffering=0)
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=logfile,
                                stderr=subprocess.STDOUT, close_fds=True, **kwargs)
    finally:
        logfile.close()           # the child holds its own handle
    try:
        created = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        created = time.time()
    return proc, created


def is_alive(pid: int | None, create_time: float | None) -> bool:
    if not pid:
        return False
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return False
        return create_time is None or abs(p.create_time() - create_time) < 2.0
    except psutil.Error:
        return False


def terminate(pid: int, create_time: float | None, *, grace: float = 8.0) -> str:
    """Hard stop of a worker (the engine recovers: committed windows are kept, the in-flight one is re-requested).
    Returns 'gone', 'terminated' or 'killed'."""
    if not is_alive(pid, create_time):
        return "gone"
    p = psutil.Process(pid)
    p.terminate()
    try:
        p.wait(timeout=grace)
        return "terminated"
    except psutil.TimeoutExpired:
        p.kill()
        try:
            p.wait(timeout=grace)
        except psutil.TimeoutExpired:
            pass
        return "killed"


def hard_kill(pid: int, create_time: float | None, *, wait: float = 8.0) -> str:
    """Immediate, uncatchable stop (SIGKILL / TerminateProcess). No cleanup runs in the worker: committed windows are safe
    in DuckDB's write-ahead log, the in-flight window is simply requested again on resume. Returns 'gone' or 'killed'."""
    if not is_alive(pid, create_time):
        return "gone"
    p = psutil.Process(pid)
    p.kill()
    try:
        p.wait(timeout=wait)
    except psutil.TimeoutExpired:
        pass
    return "killed"


def free_bytes(path: Path) -> int:
    import shutil
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free
