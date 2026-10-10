"""Worker entry point: runs ONE engine command (``stockdata.cli.main``) under supervision.

    python -m stockdata.web.worker --control-dir DIR -- <engine arguments...>

The wrapper adds no download logic. It (1) switches on the engine's optional run control (stop request file +
progress snapshot), (2) keeps a heartbeat, (3) turns SIGTERM/SIGBREAK into a *graceful* stop request, and (4) writes
``result.json`` so a supervisor that was not running when the worker finished can still learn how it ended.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import traceback
from pathlib import Path

from stockdata import jobcontrol
from stockdata.timeutil import utcnow


def write_result(directory: Path, **fields) -> None:
    fields["finished_at"] = utcnow().isoformat(timespec="seconds")
    fields["pid"] = os.getpid()
    tmp = directory / "result.tmp"
    tmp.write_text(json.dumps(fields))
    os.replace(tmp, directory / "result.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stockdata.web.worker")
    ap.add_argument("--control-dir", required=True)
    ap.add_argument("engine_args", nargs=argparse.REMAINDER)
    ns = ap.parse_args(argv)
    engine_args = [a for a in ns.engine_args if a != "--"] if ns.engine_args[:1] == ["--"] else ns.engine_args
    directory = Path(ns.control_dir)
    os.environ[jobcontrol.ENV_VAR] = str(directory)
    control = jobcontrol.JobControl.from_env()      # the same object the engine will use
    control.report("phase", phase="starting")

    stopping = threading.Event()

    def graceful(signum, _frame):
        if stopping.is_set():
            raise KeyboardInterrupt       # a second signal means "now": the engine's own interrupt path
        stopping.set()
        control.request_stop("signal")

    for name in ("SIGTERM", "SIGBREAK", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, graceful)
            except (ValueError, OSError):
                pass

    done = threading.Event()

    def beat():
        while not done.wait(2.0):
            control.heartbeat()

    threading.Thread(target=beat, daemon=True).start()

    code, message = 1, None
    try:
        from stockdata.cli import main as engine_main
        code = engine_main(engine_args)
    except SystemExit as exc:               # argparse errors and the engine's explicit refusals
        if isinstance(exc.code, int):
            code = exc.code
        else:
            code, message = 1, str(exc.code)
            print(message, file=sys.stderr)
    except KeyboardInterrupt:
        code, message = 130, "interrupted"
    except BaseException:                   # noqa: BLE001 - the supervisor needs the outcome whatever happened
        message = traceback.format_exc(limit=8)
        print(message, file=sys.stderr)
        code = 1
    finally:
        done.set()
    control.report("phase", phase="finished")
    write_result(directory, exit_code=code, message=(message or "")[:2000])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
