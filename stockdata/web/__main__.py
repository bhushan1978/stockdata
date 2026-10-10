"""Application control: ``python -m stockdata.web start | stop | status | serve``.

start   starts the application in the background (or opens the one that is already running), then opens the browser
stop    stops the application server safely; running downloads are paused at a checkpoint first, or deliberately left running
status  shows whether the server is up and which download workers are alive (works when the server is down)
serve   runs the server in this console (what `start` launches in the background; useful for development)

``start_stockdata.bat`` and ``stop_stockdata.bat`` are thin wrappers around the first two.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from stockdata.web import bootstrap, lifecycle
from stockdata.web.redact import redact
from stockdata.web.settings import DEFAULT_PORT, WebSettings

PORT_TRIES = 20
START_TIMEOUT = 60.0
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


# ------------------------------------------------------------------------------------------------ helpers
def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0 if sys.platform == "win32" else 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _call(port: int, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict | list:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-StockData-UI": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def server_state(settings: WebSettings) -> tuple[str, dict | None]:
    """('running', info) | ('unresponsive', info) | ('none', None): is a server of THIS data directory up?"""
    info = lifecycle.read_info(settings.server_info)
    if info:
        try:
            health = _call(info["port"], "GET", "/api/health", timeout=4)
            if health.get("pid") == info.get("pid"):
                return "running", info
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            pass
    probe = lifecycle.ServerLock(settings.server_lock)
    if probe.acquire():                              # nobody holds the lock: any info file is stale
        probe.release()
        if info:
            lifecycle.remove_info(settings.server_info)
        return "none", None
    return "unresponsive", info


def _tail(path: Path, lines: int = 25) -> str:
    try:
        return redact("\n".join(path.read_bytes()[-20_000:].decode("utf-8", "replace").splitlines()[-lines:]))
    except OSError:
        return ""


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:                                # noqa: BLE001 - a missing browser must never fail a start
        pass


# ------------------------------------------------------------------------------------------------ start
def cmd_start(args) -> int:
    from stockdata.web.process import is_alive, spawn_detached

    settings = WebSettings.load(data_dir=args.data_dir, port=args.port or DEFAULT_PORT)
    fe = bootstrap.check_frontend(settings.repo_root / "webui" / "dist")
    if not fe["ok"]:
        print("[StockData] The prebuilt web interface is missing or incomplete: " + "; ".join(fe["problems"]), file=sys.stderr)
        print("            Restore webui/dist (for example `git checkout -- webui/dist`) or download the project again.", file=sys.stderr)
        return 1
    state, info = server_state(settings)
    if state == "running":
        url = info["url"]
        print(f"[StockData] The application is already running (pid {info['pid']}) at {url}")
        print("            No second server was started. Opening it in your browser.")
        if not args.no_browser:
            _open_browser(url)
        return 0
    if state == "unresponsive":
        print(f"[StockData] A server for this data directory holds the lock (pid {(info or {}).get('pid', '?')}) but does not answer.", file=sys.stderr)
        print("            It may still be starting. Wait a few seconds and run this again; if it stays stuck run:  stop_stockdata.bat --force", file=sys.stderr)
        return 3
    preferred = args.port or DEFAULT_PORT
    port = next((p for p in range(preferred, preferred + PORT_TRIES) if _port_free("127.0.0.1", p)), None)
    if port is None:
        print(f"[StockData] No free port between {preferred} and {preferred + PORT_TRIES - 1}.", file=sys.stderr)
        print(f"            Choose another start:  start_stockdata.bat {preferred + 100}", file=sys.stderr)
        return 4
    if port != preferred:
        print(f"[StockData] Port {preferred} is used by another program; using port {port} instead.")
        print(f"            (find the other program with:  netstat -ano | findstr :{preferred})")
    if args.foreground:
        args.port = port
        return cmd_serve(args)

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    console_log = settings.log_dir / "server_console.log"
    argv = [sys.executable, "-m", "stockdata.web", "--data-dir", str(settings.data_dir), "serve", "--port", str(port)]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    print(f"[StockData] Starting the application (data directory {settings.data_dir}) ...")
    proc, created = spawn_detached(argv, cwd=settings.repo_root, env=env, log_path=console_log)
    url = f"http://127.0.0.1:{port}/"
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"[StockData] The server stopped during start-up (exit code {proc.returncode}). Last lines of {console_log}:", file=sys.stderr)
            print(_tail(console_log) or "  (no output)", file=sys.stderr)
            return 1
        try:
            if _call(port, "GET", "/api/health", timeout=2).get("pid") == proc.pid:
                break
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            pass
        time.sleep(0.4)
    else:
        print(f"[StockData] The server did not become ready within {int(START_TIMEOUT)} seconds. Last lines of {console_log}:", file=sys.stderr)
        print(_tail(console_log) or "  (no output)", file=sys.stderr)
        return 1
    health = _call(port, "GET", "/api/health")
    print(f"[StockData] Running at {url}   (version {health['version']}, process {proc.pid})")
    if health.get("workers_active"):
        print(f"[StockData] {health['workers_active']} download worker(s) were still running and have been re-adopted.")
    print("[StockData] The application keeps running in the background; you can close this window.")
    print("[StockData] To shut it down later run stop_stockdata.bat (it pauses downloads safely first).")
    if not args.no_browser:
        _open_browser(url)
    return 0


# ------------------------------------------------------------------------------------------------ serve
def cmd_serve(args) -> int:
    import uvicorn
    from stockdata.web.api import create_app

    if args.host not in LOOPBACK:
        print("Refusing to bind to a non-loopback address: this application has no login and is local-only by design.", file=sys.stderr)
        return 2
    settings = WebSettings.load(data_dir=args.data_dir, host=args.host, port=args.port, dev=getattr(args, "dev", False))
    settings.web_dir.mkdir(parents=True, exist_ok=True)
    lock = lifecycle.ServerLock(settings.server_lock)
    if not lock.acquire():
        info = lifecycle.read_info(settings.server_info) or {}
        print(f"Another StockData server already uses this data directory ({settings.data_dir}).\n"
              f"  pid {info.get('pid', '?')}, http://127.0.0.1:{info.get('port', '?')}\n"
              "Open that address, or run stop_stockdata.bat first. Download workers are not affected by either.", file=sys.stderr)
        return 3
    if not _port_free(settings.host, settings.port):
        lock.release()
        print(f"Port {settings.port} is already in use by another program.\n"
              f"  Use a different port:  start_stockdata.bat {settings.port + 1}\n"
              f"  or find the owner:     netstat -ano | findstr :{settings.port}", file=sys.stderr)
        return 4

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.handlers.RotatingFileHandler(settings.log_dir / "server.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
                logging.StreamHandler(sys.stderr)]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        root.addHandler(h)
    log = logging.getLogger("stockdata.web")

    app = create_app(settings)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    app.state.server = server
    url = f"http://127.0.0.1:{settings.port}/"
    lifecycle.write_info(settings.server_info, port=settings.port, url=url, data_dir=str(settings.data_dir), host=settings.host)
    active = app.state.sup._active_jobs()
    print(f"\nStock Data Management Center {settings.version} ({settings.commit})\n  URL        {url}\n  data dir   {settings.data_dir}  [{settings.data_dir_source}]"
          + (f"\n  workers    {len(active)} download worker(s) re-adopted after start" if active else ""))
    print("  Stopping this server does NOT stop running downloads (separate processes). Use stop_stockdata.bat / the Settings page.\n", flush=True)
    log.info("server starting on %s (data dir %s)", url, settings.data_dir)

    if getattr(args, "open_browser", False):
        def opener():
            for _ in range(100):
                if server.started:
                    _open_browser(url)
                    return
                time.sleep(0.1)
        threading.Thread(target=opener, daemon=True).start()
    try:
        server.run()
    finally:
        still = app.state.sup._active_jobs()
        if still:
            print(f"\n{len(still)} download worker(s) are STILL RUNNING and will keep downloading: "
                  + ", ".join(f"#{j['id']}" for j in still) + ". Start the application again to monitor or stop them.")
        lifecycle.remove_info(settings.server_info)
        lock.release()
        log.info("server stopped")
    return 0


# ------------------------------------------------------------------------------------------------ stop
def _active_jobs(port: int) -> list[dict]:
    return _call(port, "GET", "/api/jobs?status=preparing,running,stopping")  # type: ignore[return-value]


def _describe(j: dict) -> str:
    p = j["progress"]
    return f"#{j['id']} {j['name']}  ({p['windows_done']:,} of {p['windows_planned'] if p['windows_planned'] is not None else '?'} windows, {j['status']})"


def _tracked_workers(settings: WebSettings) -> list[dict]:
    """Download workers recorded as active in the control database whose process is really alive (works without the server)."""
    from stockdata.web.process import is_alive
    from stockdata.web.store import ACTIVE, Store
    if not settings.control_db.exists():
        return []
    store = Store(settings.control_db)
    try:
        out = []
        for r in store.all(f"SELECT id, name, status FROM jobs WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE):
            run = store.one("SELECT pid, pid_create_time FROM job_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1", (r["id"],))
            if run and is_alive(run["pid"], run["pid_create_time"]):
                out.append({**r, "pid": run["pid"], "create_time": run["pid_create_time"]})
        return out
    finally:
        store.close()


def _confirm_force(args, what: str, inp=input) -> bool:
    print("\n" + "!" * 78)
    print("FORCE STOP is for RECOVERY only (a hung or unreachable application).")
    print(what)
    print("Every window already saved is safe and will not be downloaded again; the window that was")
    print("in flight is simply requested again. The next start marks the affected jobs 'Interrupted'")
    print("and you resume them from the application.")
    print("!" * 78)
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("Not confirmed (no terminal to ask). Add --yes to confirm.")
        return False
    return inp("Type FORCE to continue: ").strip() == "FORCE"


def _wait_gone(settings: WebSettings, info: dict, seconds: float = 30.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            _call(info["port"], "GET", "/api/health", timeout=2)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            return True
        time.sleep(0.5)
    return False


def _pause_and_wait(port: int, wait: float, out=print) -> bool:
    """Ask every active job to pause at its next checkpoint and wait until none is active. False on timeout."""
    _call(port, "POST", "/api/jobs/pause-all", {})
    out("Waiting for each running download to finish the window it is on (this can take a minute) ...")
    end, last = time.time() + wait, 0.0
    while time.time() < end:
        active = _active_jobs(port)
        if not active:
            return True
        if time.time() - last >= 5:
            out("  still finishing: " + "; ".join(_describe(j) for j in active))
            last = time.time()
        time.sleep(1.0)
    return False


def cmd_stop(args, inp=input) -> int:
    from stockdata.web.process import hard_kill
    import psutil

    settings = WebSettings.load(data_dir=args.data_dir, port=args.port)
    state, info = server_state(settings)
    pause = args.pause_downloads or args.stop_jobs
    if sum(map(bool, (pause, args.keep_downloads, args.force))) > 1:
        print("Choose only one of --pause-downloads, --keep-downloads, --force.", file=sys.stderr)
        return 1

    # ---- no responsive server
    if state != "running":
        workers = _tracked_workers(settings)
        if state == "none":
            print("[StockData] The application server is not running.")
        else:
            print(f"[StockData] A server (pid {(info or {}).get('pid', '?')}) holds the lock but does not answer.")
        for w in workers:
            print(f"[StockData] Download job #{w['id']} {w['name']} is STILL RUNNING (worker pid {w['pid']}); it was not touched.")
        if state == "unresponsive" and not args.force:
            print("            Wait a moment and retry, or use  stop_stockdata.bat --force  (recovery only).")
            return 1
        if args.force:
            if not _confirm_force(args, "This ends the unresponsive server process immediately" + (" and the download workers listed above." if args.kill_workers else ".")):
                return 1
            if state == "unresponsive" and info:
                try:
                    p = psutil.Process(info["pid"])
                    p.kill()
                    p.wait(timeout=10)
                    print(f"[StockData] Server process {info['pid']} was force-stopped.")
                except psutil.Error as exc:
                    print(f"[StockData] Could not stop the server process: {exc}", file=sys.stderr)
                    return 1
                lifecycle.remove_info(settings.server_info)
            if args.kill_workers:
                for w in workers:
                    print(f"[StockData] Download worker {w['pid']} (job #{w['id']}): {hard_kill(w['pid'], w['create_time'])}")
        elif workers:
            print("            Start the application again to monitor or pause them.")
        return 0

    # ---- a responsive server
    port = info["port"]
    active = _active_jobs(port)
    if args.force:
        what = ("This ends the application server process immediately, without a clean shutdown"
                + (" and also ends the running download workers immediately." if args.kill_workers else "; download workers keep running."))
        if not _confirm_force(args, what):
            return 1
        workers = _tracked_workers(settings)
        try:
            p = psutil.Process(info["pid"])
            p.kill()
            p.wait(timeout=10)
            print(f"[StockData] Server process {info['pid']} was force-stopped.")
        except psutil.Error as exc:
            print(f"[StockData] Could not stop the server process: {exc}", file=sys.stderr)
            return 1
        lifecycle.remove_info(settings.server_info)
        if args.kill_workers:
            for w in workers:
                print(f"[StockData] Download worker {w['pid']} (job #{w['id']}): {hard_kill(w['pid'], w['create_time'])}")
        else:
            for w in workers:
                print(f"[StockData] Download job #{w['id']} is STILL RUNNING (worker pid {w['pid']}); start the application to manage it.")
        print("[StockData] The next start recovers the state: finished jobs are completed, killed ones become 'Interrupted' (resume them from the Job Queue).")
        return 0

    strategy = "pause" if pause else "keep" if args.keep_downloads else None
    if active and strategy is None:
        print(f"\n{len(active)} download job(s) are running right now:")
        for j in active:
            print("   " + _describe(j))
        print("\nWhat should happen to them?\n"
              "  [P] Pause them at the next safe checkpoint, then stop the server   (recommended)\n"
              "        the window each one is on is finished and saved first; nothing is lost; resume later from the application\n"
              "  [K] Keep them running and stop only the web server\n"
              "        downloads continue in the background; start the application again to watch or pause them\n"
              "  [C] Cancel - change nothing")
        if not sys.stdin.isatty():
            print("\nNo terminal to ask. Re-run with --pause-downloads or --keep-downloads.", file=sys.stderr)
            return 2
        choice = inp("Choice [P/K/C]: ").strip().lower()
        strategy = {"p": "pause", "k": "keep"}.get(choice)
        if strategy is None:
            print("Cancelled; the application is still running.")
            return 1
    if active and strategy == "pause":
        if not _pause_and_wait(port, args.wait):
            print(f"\n[StockData] The downloads did not reach a checkpoint within {int(args.wait)} seconds, so the server was NOT stopped.", file=sys.stderr)
            print("            They are still finishing their current window (a long Retry-After from Dhan can take minutes). Run this again to keep waiting,", file=sys.stderr)
            print("            or use --keep-downloads to stop only the server.", file=sys.stderr)
            return 3
        print("[StockData] All downloads are paused at a checkpoint.")
    out = _call(port, "POST", "/api/system/shutdown", {"confirm": True}, timeout=60)
    if not _wait_gone(settings, info):
        print("[StockData] The server accepted the shutdown request but is still answering; check again in a moment.", file=sys.stderr)
        return 1
    print("[StockData] The application server has stopped.")
    still = out.get("workers_still_running") or []
    if still:
        print(f"[StockData] WARNING: {len(still)} download job(s) are STILL RUNNING in the background:")
        for w in still:
            print(f"            job #{w['id']} {w['name']} ({w['status']})")
        print("            Start the application again to monitor or pause them.")
    elif active:
        print("[StockData] The paused downloads wait in the Job Queue; start the application and press Resume when you are ready.")
    else:
        print("[StockData] No download was running.")
    return 0


# ------------------------------------------------------------------------------------------------ status
def cmd_status(args) -> int:
    settings = WebSettings.load(data_dir=args.data_dir, port=args.port)
    state, info = server_state(settings)
    print(f"data directory: {settings.data_dir}")
    if state == "running":
        print(f"server: running at {info['url']} (pid {info['pid']})")
    elif state == "unresponsive":
        print(f"server: NOT RESPONDING (pid {(info or {}).get('pid', '?')} holds the lock)")
    else:
        print("server: not running")
    workers = _tracked_workers(settings)
    if not workers:
        print("download workers: none active")
    for w in workers:
        print(f"  job #{w['id']} {w['name']}: {w['status']}, worker pid {w['pid']} ALIVE")
    return 0


# ------------------------------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m stockdata.web", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", help="data folder (default: STOCKDATA_DIR, stockdata_web.json, or <repository>/data)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="start in the background, or open the already-running application")
    p.add_argument("--port", type=int, help=f"preferred port (default {DEFAULT_PORT}); the next free one is used if it is taken")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    p.add_argument("--foreground", action="store_true", help="run the server in this console instead of the background")
    p.set_defaults(func=cmd_start, host="127.0.0.1", dev=False, open_browser=True)

    p = sub.add_parser("serve", help="run the server in this console")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--open-browser", action="store_true", help="open the default browser once the server is up")
    p.add_argument("--dev", action="store_true", help="also accept the Vite dev server origin (http://localhost:5173)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("stop", help="stop the application server safely")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    g = p.add_argument_group("what happens to running downloads (asked interactively when none is given)")
    g.add_argument("--pause-downloads", action="store_true", help="pause them at the next checkpoint, wait, then stop the server (recommended)")
    g.add_argument("--stop-jobs", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--keep-downloads", action="store_true", help="leave them running and stop only the server")
    g.add_argument("--wait", type=float, default=900.0, help="seconds to wait for downloads to reach a checkpoint (default 900)")
    g = p.add_argument_group("recovery only")
    g.add_argument("--force", action="store_true", help="end the server process immediately (needs confirmation)")
    g.add_argument("--kill-workers", action="store_true", help="with --force: also end download workers immediately")
    p.add_argument("--yes", action="store_true", help="confirm --force without asking")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", help="show the server and download workers")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.set_defaults(func=cmd_status)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
