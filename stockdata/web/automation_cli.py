"""Headless automation entry point:  ``python -m stockdata.web automation run|status|install|remove|enable|disable|xml``.

``run`` is what the Windows Task Scheduler executes. It needs no browser and starts NO web server:

* if the application is already running for this data directory, the run is handed to it (its scheduler does the work, so
  there is never a second supervisor on the same queue);
* otherwise the run takes the data directory's server lock for its duration (so the application cannot start a second
  supervisor underneath it), runs the same orchestration in this process, and releases everything when it ends;
* a second scheduled instance finds the automation lock taken and exits at once (exit code 75).

Exit codes: 0 completed or skipped, 2 completed with warnings, 3 ACTION REQUIRED (credential), 4 request budget, 5 stopped,
6 disk space, 7 integrity check failed, 8 the application is not responding, 75 another run is in progress.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
import urllib.error

from stockdata.web import lifecycle
from stockdata.web.automation import (EXIT_BUSY, EXIT_NO_SERVER, EXIT_OK, EXIT_STOPPED, AutomationRunner, AutomationService, load_config)
from stockdata.web.redact import redact
from stockdata.web.settings import WebSettings

POLL_SECONDS = 3.0


class _Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def _logging(settings: WebSettings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(settings.log_dir / "automation.log", maxBytes=3_000_000,
                                                                            backupCount=5, encoding="utf-8")]
    if sys.stderr is not None:                       # pythonw.exe (no console) has no stderr
        handlers.append(logging.StreamHandler(sys.stderr))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(_Redact())
        root.addHandler(h)


def say(text: str = "") -> None:
    logging.getLogger("stockdata.automation").info(text)
    if sys.stdout is not None:
        print(text, flush=True)


def _summary_lines(result_status: str, summary: dict) -> list[str]:
    lines = [f"automatic run {result_status}: phase {summary.get('phase')}, trigger {summary.get('trigger')}"]
    for name, rec in (summary.get("timeframes") or {}).items():
        lines.append(f"  {name}: {rec.get('status')} via {rec.get('provider')}"
                     + (f" ({rec.get('message')})" if rec.get("message") else "") + (f", {rec['rounds']} round(s)" if rec.get("rounds") else ""))
    for note in summary.get("notes") or []:
        lines.append(f"  note: {note}")
    if summary.get("requests"):
        lines.append("  requests: " + ", ".join(f"{k} {v:,}" for k, v in summary["requests"].items()) + f"; candles added {summary.get('rows_inserted', 0):,}")
    return lines


def cmd_run(args) -> int:
    from stockdata.web.__main__ import _call, server_state
    from stockdata.web.api import create_app

    settings = WebSettings.load(data_dir=args.data_dir)
    settings.web_dir.mkdir(parents=True, exist_ok=True)
    _logging(settings)
    auto_lock = lifecycle.ServerLock(settings.web_dir / "automation.lock")
    if not auto_lock.acquire():
        say("[automation] another automatic run is already in progress; this one exits without doing anything.")
        return EXIT_BUSY
    try:
        state, info = server_state(settings)
        if state == "running":
            return _delegate(settings, info, args, _call)
        if state != "none":
            say(f"[automation] a StockData process holds the data directory but does not answer ({state}); nothing was started.")
            return EXIT_NO_SERVER
        srv_lock = lifecycle.ServerLock(settings.server_lock)
        if not srv_lock.acquire():
            say("[automation] the application started a moment ago and owns the data directory; run again in a minute.")
            return EXIT_NO_SERVER
        try:
            lifecycle.write_info(settings.server_info, kind="automation", data_dir=str(settings.data_dir), phase=args.phase)
            app = create_app(settings, start_scheduler=False)
            sup, store = app.state.sup, app.state.store
            try:
                runner = AutomationRunner(sup, store, app.state.data, app.state.gate, settings, tick=sup.tick)
                say(f"[automation] {args.phase} run ({args.trigger}) starting; data directory {settings.data_dir}")
                result = runner.run(phase=args.phase, trigger=args.trigger)
                sup.tick()                           # a backfill paused to make room restarts now instead of waiting for the next login
                for line in _summary_lines(result.status, result.summary):
                    say(line)
                return result.exit_code
            finally:
                store.close()
        finally:
            lifecycle.remove_info(settings.server_info)
            srv_lock.release()
    finally:
        auto_lock.release()


def _delegate(settings: WebSettings, info: dict, args, call) -> int:
    """The application is running: ask it to do the run (its scheduler, its ledger, its gate) and follow it to the end."""
    port = info["port"]
    say(f"[automation] the application is running (pid {info['pid']}); the {args.phase} run is handed to it.")
    try:
        started = call(port, "POST", "/api/automation/run", {"phase": args.phase, "confirm": True}, timeout=30)
    except urllib.error.HTTPError as exc:
        say(f"[automation] the application refused the run: {redact(exc.read().decode('utf-8', 'replace'))[:300]}")
        return EXIT_BUSY if exc.code == 409 else EXIT_STOPPED
    except (urllib.error.URLError, OSError, TimeoutError):
        say("[automation] the application stopped answering; nothing was started.")
        return EXIT_NO_SERVER
    run_id = started.get("run_id")
    deadline = time.time() + load_config_hours(settings) * 3600
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            runs = call(port, "GET", "/api/automation/runs?limit=5")
        except (urllib.error.URLError, OSError, TimeoutError):
            continue
        run = next((r for r in runs if r["id"] == run_id), None) if run_id else (runs[0] if runs else None)
        if run and run["status"] != "running":
            for line in _summary_lines(run["status"], run["summary"]):
                say(line)
            return run["exit_code"] if run["exit_code"] is not None else EXIT_OK
    say("[automation] gave up waiting for the application's run; it continues there.")
    return EXIT_STOPPED


def load_config_hours(settings: WebSettings) -> int:
    from stockdata.web.store import Store
    store = Store(settings.control_db)
    try:
        return load_config(store).max_runtime_hours + 1
    finally:
        store.close()


def _service(args) -> tuple[AutomationService, object]:
    from stockdata.web.api import create_app
    settings = WebSettings.load(data_dir=args.data_dir)
    settings.web_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings, start_scheduler=False)
    return app.state.automation, app


def cmd_manage(args) -> int:
    svc, app = _service(args)
    try:
        if args.action == "status":
            st = svc.status()
            print(json.dumps({k: st[k] for k in ("config", "tasks", "last_success", "missed_executions", "recent_problems", "recovery")}, indent=2, default=str))
        elif args.action == "xml":
            for t in svc.xml_preview():
                print(f"--- {t['task']}\n{t['xml']}")
        else:
            out = {"install": svc.install, "remove": svc.remove}.get(args.action) or (lambda: svc.set_enabled(args.action == "enable"))
            st = out()
            for t in st["tasks"]:
                print(f"{t['task']}: registered={t['registered']} enabled={t['enabled']} state={t['state']}")
        return 0
    finally:
        app.state.store.close()


def register(sub) -> None:
    p = sub.add_parser("automation", help="unattended updates: run one, or manage the Windows scheduled tasks")
    s = p.add_subparsers(dest="action", required=True)
    r = s.add_parser("run", help="run one automatic update (what the scheduled task executes; no browser or server needed)")
    r.add_argument("--phase", choices=["post-close", "reconcile"], default="post-close")
    r.add_argument("--trigger", choices=["scheduled", "manual"], default="manual")
    r.set_defaults(func=cmd_run)
    for name, text in (("status", "show the configuration, tasks and recent runs"), ("install", "create or update the scheduled tasks"),
                       ("remove", "remove the scheduled tasks"), ("enable", "enable the scheduled tasks"),
                       ("disable", "disable the scheduled tasks"), ("xml", "print the task definitions (preview)")):
        q = s.add_parser(name, help=text)
        q.set_defaults(func=cmd_manage)
