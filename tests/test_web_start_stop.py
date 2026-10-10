"""start_stockdata.bat / stop_stockdata.bat do their work in Python (`python -m stockdata.web start|stop`). These tests run those
commands for real - real server processes, real download workers against the local fake Dhan server."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from argparse import Namespace
from pathlib import Path

import psutil
import pytest

from conftest import load_universe
from stockdata.budget import read_usage
from stockdata.db import CandleDB
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.web import __main__ as web_main
from stockdata.web import bootstrap, lifecycle
from stockdata.web.settings import WebSettings
from web_helpers import intraday_spec, wait_for

REPO = Path(__file__).parent.parent
PY = sys.executable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    """What the two .bat files do, driven from Python: start / stop / status as separate processes."""

    def __init__(self, tmp_path: Path, fake):
        self.data = tmp_path / "data"
        self.data.mkdir()
        for tf in (DAILY, INTRADAY_15):
            with CandleDB(tf, self.data / tf.db_name) as db:
                load_universe(db)
        self.env = {**os.environ, "DHAN_ACCESS_TOKEN": "test-token", "DHAN_CLIENT_ID": "1000000001", "DHAN_BASE_URL": fake.base_url,
                    "STOCKDATA_RPS": "1000", "STOCKDATA_ENV_FILE": str(tmp_path / "nowhere.env")}
        self.env.pop("STOCKDATA_DIR", None)
        self.port = None

    def run(self, *args, check=None, timeout=120):
        r = subprocess.run([PY, "-m", "stockdata.web", "--data-dir", str(self.data), *args], cwd=REPO, env=self.env, capture_output=True, text=True, timeout=timeout)
        if check is not None:
            assert r.returncode == check, f"{args}: exit {r.returncode}\n{r.stdout}\n{r.stderr}"
        return r

    def start(self, port=None, expect=0):
        port = port or free_port()
        r = self.run("start", "--port", str(port), "--no-browser", check=expect)
        self.port = port
        return r

    def api(self, method, path, body=None, port=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port or self.port}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-StockData-UI": "1"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:                     # show the server's explanation, not just the status line
            raise AssertionError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:600]}") from None

    def info(self):
        return lifecycle.read_info(self.data / "web" / "server.json")

    def servers(self):
        return [p for p in psutil.process_iter(["cmdline"]) if p.info["cmdline"] and "stockdata.web" in " ".join(p.info["cmdline"]) and "serve" in p.info["cmdline"] and str(self.data) in " ".join(p.info["cmdline"])]

    def workers(self):
        return [p for p in psutil.process_iter(["cmdline"]) if p.info["cmdline"] and "stockdata.web.worker" in " ".join(p.info["cmdline"]) and str(self.data) in " ".join(p.info["cmdline"])]

    def cleanup(self):
        for p in self.workers() + self.servers():
            try:
                p.kill()
            except psutil.Error:
                pass


@pytest.fixture
def app(tmp_path, fake):
    a = App(tmp_path, fake)
    yield a
    a.cleanup()


def start_job(app, spec=None):
    made = app.api("POST", "/api/jobs", {"spec": spec or intraday_spec(), "start": "now", "confirm": True})
    return made["job"]["id"]


def job(app, jid):
    return app.api("GET", f"/api/jobs/{jid}")


def windows_done(app, jid):
    return job(app, jid)["progress"]["run_counts"].get("windows_done", 0)


# --------------------------------------------------------------------------------------------- start
def test_start_runs_the_server_in_the_background_and_a_second_start_only_opens_it(app):
    r = app.start()
    assert "Running at" in r.stdout and "keeps running in the background" in r.stdout
    info = app.info()
    assert info["port"] == app.port and Path(info["data_dir"]) == app.data and app.api("GET", "/api/health")["pid"] == info["pid"]
    assert psutil.pid_exists(info["pid"]) and len(app.servers()) == 1               # the start command has exited, the server has not
    again = app.run("start", "--port", str(app.port), "--no-browser", check=0)
    assert "already running" in again.stdout and "No second server was started" in again.stdout
    assert len(app.servers()) == 1 and app.info()["pid"] == info["pid"]
    assert app.run("status", check=0).stdout.count("server: running") == 1


def test_start_uses_the_next_free_port_when_the_default_is_taken_and_says_so(app):
    busy = socket.socket(); busy.bind(("127.0.0.1", 0)); busy.listen(1)
    taken = busy.getsockname()[1]
    try:
        r = app.start(port=taken)
        assert app.port == taken and app.info()["port"] != taken and f"Port {taken} is used by another program" in r.stdout
        assert app.api("GET", "/api/health", port=app.info()["port"])["status"] == "ok"
    finally:
        busy.close()


def test_start_explains_a_failed_start_with_the_server_output(app, monkeypatch, capsys):
    import stockdata.web.process as process
    real = process.spawn_detached
    monkeypatch.setattr(process, "spawn_detached", lambda argv, cwd, env, log_path: real([PY, "-c", "print('boom: simulated start-up failure'); raise SystemExit(3)"], cwd=cwd, env=env, log_path=log_path))
    rc = web_main.main(["--data-dir", str(app.data), "start", "--port", str(free_port()), "--no-browser"])
    err = capsys.readouterr().err
    assert rc == 1 and "stopped during start-up" in err and "boom: simulated start-up failure" in err and app.info() is None


def test_start_refuses_cleanly_when_the_prebuilt_interface_is_missing(app, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "check_frontend", lambda dist=None: {"ok": False, "path": "x", "problems": ["webui/dist/index.html is missing"]})
    rc = web_main.main(["--data-dir", str(app.data), "start", "--no-browser"])
    err = capsys.readouterr().err
    assert rc == 1 and "index.html is missing" in err and "git checkout" in err and not app.servers()


def test_data_is_always_kept_in_the_absolute_data_directory_even_for_a_relative_one(tmp_path, fake):
    work = tmp_path / "elsewhere"
    work.mkdir()
    env = {**os.environ, "PYTHONPATH": str(REPO), "DHAN_ACCESS_TOKEN": "test-token", "DHAN_CLIENT_ID": "1000000001", "DHAN_BASE_URL": fake.base_url,
           "STOCKDATA_RPS": "1000", "STOCKDATA_ENV_FILE": str(tmp_path / "x.env")}
    env.pop("STOCKDATA_DIR", None)
    port = free_port()
    r = subprocess.run([PY, "-m", "stockdata.web", "--data-dir", "reldata", "start", "--port", str(port), "--no-browser"], cwd=work, env=env, capture_output=True, text=True, timeout=120)
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        absolute = (work / "reldata").resolve()
        assert lifecycle.read_info(absolute / "web" / "server.json")["data_dir"] == str(absolute)
        assert (absolute / "web" / "app_control.sqlite").exists() and not (REPO / "reldata").exists()
        s = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/settings", headers={"X-StockData-UI": "1"})).read()
        paths = json.loads(s)["paths"]
        assert paths["data_dir"] == str(absolute) and paths["ledger"] == str(absolute / "request_ledger.sqlite") and Path(paths["daily_db"]).is_absolute()
    finally:
        subprocess.run([PY, "-m", "stockdata.web", "--data-dir", str(work / "reldata"), "stop", "--pause-downloads"], cwd=work, env=env, capture_output=True, timeout=60)


# --------------------------------------------------------------------------------------------- stop
def test_stop_never_decides_for_you_when_a_download_is_running(app, fake):
    fake.delay = 0.4
    app.start()
    jid = start_job(app)
    wait_for(lambda: windows_done(app, jid) >= 1, timeout=60)
    r = app.run("stop")                                                                  # no terminal, no choice made
    assert r.returncode == 2 and "download job(s) are running" in r.stdout and f"#{jid}" in r.stdout and "--pause-downloads" in r.stderr
    assert app.info() is not None and job(app, jid)["status"] == "running" and len(app.workers()) == 1   # nothing happened


def test_pause_first_stop_pauses_at_a_checkpoint_stops_the_server_and_a_restart_resumes_without_repeats(app, fake):
    fake.delay = 0.3
    app.start()
    jid = start_job(app)
    wait_for(lambda: windows_done(app, jid) >= 2, timeout=60)
    r = app.run("stop", "--pause-downloads")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All downloads are paused at a checkpoint" in r.stdout and "application server has stopped" in r.stdout
    assert app.info() is None and not app.servers() and not app.workers()                # nothing is left running
    done_before = None
    app.start()                                                                           # a later restart
    j = job(app, jid)
    assert j["status"] == "paused" and j["exit_code"] == 5 and "resume" in j["actions"]  # waiting for you; not resumed behind your back
    done_before = j["windows_done"]
    assert 2 <= done_before < j["windows_planned"]
    fake.delay = 0
    app.api("POST", f"/api/jobs/{jid}/resume", {})
    wait_for(lambda: job(app, jid)["status"] == "completed", timeout=90)
    final = job(app, jid)
    assert final["windows_done"] == final["windows_planned"] == sum(read_usage(app.data / "request_ledger.sqlite").values())   # each window requested exactly once
    assert (app.data / "request_ledger.sqlite").exists() and (app.data / "web" / "app_control.sqlite").exists()


def test_keep_downloads_stops_only_the_server_and_the_next_start_readopts_the_worker(app, fake):
    fake.delay = 0.4
    app.start()
    jid = start_job(app)
    wait_for(lambda: windows_done(app, jid) >= 1, timeout=60)
    pid = job(app, jid)["worker"]["pid"]
    r = app.run("stop", "--keep-downloads", check=0)
    assert "STILL RUNNING" in r.stdout and f"job #{jid}" in r.stdout
    assert not app.servers() and psutil.pid_exists(pid)                                   # the worker was not touched
    assert "ALIVE" in app.run("status", check=0).stdout
    app.start()
    j = job(app, jid)
    assert j["worker"]["pid"] == pid and j["worker"]["alive"] and len(app.workers()) == 1
    fake.delay = 0
    wait_for(lambda: job(app, jid)["status"] == "completed", timeout=90)


def test_interactive_prompt_offers_three_choices(app, fake, monkeypatch, capsys):
    fake.delay = 0.3
    app.start()
    jid = start_job(app)
    wait_for(lambda: windows_done(app, jid) >= 1, timeout=60)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    args = lambda **kw: Namespace(data_dir=str(app.data), port=app.port, pause_downloads=False, stop_jobs=False, keep_downloads=False, force=False,
                                  kill_workers=False, yes=False, wait=120.0, **kw)
    assert web_main.cmd_stop(args(), inp=lambda prompt: "c") == 1                          # cancel
    out = capsys.readouterr().out
    assert "[P] Pause them at the next safe checkpoint" in out and "[K] Keep them running" in out and "[C] Cancel" in out and "Cancelled" in out
    assert app.info() is not None and job(app, jid)["status"] == "running"
    assert web_main.cmd_stop(args(), inp=lambda prompt: "") == 1                           # an empty answer is not consent either
    assert web_main.cmd_stop(args(), inp=lambda prompt: "p") == 0                          # pause then stop
    assert app.info() is None and not app.workers()


def test_stop_times_out_without_stopping_the_server_if_a_checkpoint_is_not_reached(app, fake):
    fake.delay = 3.0                                                                       # each request takes 3 s
    app.start()
    jid = start_job(app)
    wait_for(lambda: job(app, jid)["status"] == "running", timeout=60)
    r = app.run("stop", "--pause-downloads", "--wait", "1")
    assert r.returncode == 3 and "server was NOT stopped" in r.stderr
    assert app.info() is not None and job(app, jid)["status"] in ("stopping", "paused")    # still pausing, never killed


def test_force_needs_confirmation_and_by_default_leaves_workers_alone(app, fake):
    fake.delay = 0.4
    app.start()
    jid = start_job(app)
    wait_for(lambda: windows_done(app, jid) >= 1, timeout=60)
    pid = job(app, jid)["worker"]["pid"]
    r = app.run("stop", "--force")
    assert r.returncode == 1 and "FORCE STOP is for RECOVERY only" in r.stdout and "Not confirmed" in r.stdout
    assert len(app.servers()) == 1                                                         # refused: nothing was killed
    r = app.run("stop", "--force", "--yes", check=0)
    assert "was force-stopped" in r.stdout and f"job #{jid} is STILL RUNNING" in r.stdout and "marks the affected jobs" in r.stdout
    assert not app.servers() and psutil.pid_exists(pid) and app.info() is None
    app.start()
    assert job(app, jid)["worker"]["alive"] is True                                        # the worker survived and is re-adopted
    fake.delay = 0
    wait_for(lambda: job(app, jid)["status"] == "completed", timeout=90)


def test_force_with_kill_workers_then_a_restart_recovers_the_job_as_interrupted_and_it_resumes(app, fake):
    fake.delay = 0.4
    app.start()
    jid = start_job(app)
    wait_for(lambda: windows_done(app, jid) >= 2, timeout=60)
    r = app.run("stop", "--force", "--kill-workers", "--yes", check=0)
    assert "Download worker" in r.stdout and not app.servers() and not app.workers()
    app.start()
    j = job(app, jid)
    assert j["status"] == "interrupted" and "resume" in j["actions"] and j["windows_done"] >= 2
    fake.delay = 0
    app.api("POST", f"/api/jobs/{jid}/resume", {})
    wait_for(lambda: job(app, jid)["status"] == "completed", timeout=90)
    final = job(app, jid)
    assert final["windows_done"] == final["windows_planned"]
    with CandleDB(INTRADAY_15, app.data / INTRADAY_15.db_name, read_only=True) as db:      # no duplicates after a hard kill
        assert db.con.execute("SELECT count(*) FROM (SELECT 1 FROM candles_15min GROUP BY exchange_segment, security_id, candle_timestamp HAVING count(*) > 1)").fetchone()[0] == 0


@pytest.mark.skipif(sys.platform == "win32", reason="uses SIGSTOP to simulate a hung server")
def test_a_hung_server_is_reported_and_only_force_stop_ends_it(app):
    app.start()
    pid = app.info()["pid"]
    os.kill(pid, signal.SIGSTOP)                                                           # frozen: holds the lock, never answers
    try:
        again = app.run("start", "--port", str(app.port), "--no-browser", timeout=60)
        assert again.returncode == 3 and "does not answer" in again.stderr                   # no duplicate is started on top of it
        plain = app.run("stop", timeout=60)
        assert plain.returncode == 1 and "does not answer" in plain.stdout and "--force" in plain.stdout
        forced = app.run("stop", "--force", "--yes", timeout=60)
        assert forced.returncode == 0 and "was force-stopped" in forced.stdout
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except OSError:
            pass
    wait_for(lambda: not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE, timeout=10)
    app.start()                                                                            # and a normal start works again
    assert app.api("GET", "/api/health")["status"] == "ok"


def test_stopping_when_nothing_runs_is_harmless(app):
    r = app.run("stop", check=0)
    assert "not running" in r.stdout
    app.start()
    assert app.run("stop", check=0).returncode == 0 and app.info() is None


# --------------------------------------------------------------------------------------------- launcher and bootstrap
def test_bootstrap_parses_requirements_and_versions(tmp_path):
    (tmp_path / "base.txt").write_text("# comment\nfoo>=1.2\nbar\n")
    (tmp_path / "top.txt").write_text("-r base.txt\nbaz>=2.0.1  # inline\n--index-url http://x\n")
    parsed = bootstrap.parse_requirements(tmp_path / "top.txt")
    assert parsed == [("foo", ">=", "1.2"), ("bar", None, None), ("baz", ">=", "2.0.1")]
    assert bootstrap._satisfied("1.10.0", ">=", "1.9") and not bootstrap._satisfied("1.8", ">=", "1.9") and bootstrap._satisfied("3.0", None, None)
    rows = bootstrap.check_requirements(REPO / "requirements-web.txt")
    assert {r["package"] for r in rows} >= {"duckdb", "pandas", "requests", "tzdata", "fastapi", "uvicorn", "psutil"} and all(r["ok"] for r in rows)


def test_ensure_installs_what_is_missing_reports_pip_failures_and_checks_the_interface(tmp_path, monkeypatch):
    req = tmp_path / "req.txt"
    req.write_text("definitely-not-installed-package-xyz>=1.0\n")
    said = []
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, "call", lambda cmd: calls.append(cmd) or 1)                 # pip fails
    assert bootstrap.ensure(req, out=said.append) == 1
    assert calls and calls[0][-2:] == ["-r", str(req)] and any("pip could not install" in s for s in said)
    said.clear(); calls.clear()
    assert bootstrap.ensure(req, install=False, out=said.append) == 1 and not calls and any("Missing or outdated" in s for s in said)
    ok = tmp_path / "ok.txt"
    ok.write_text("pytest>=1\n")
    monkeypatch.setattr(bootstrap, "check_frontend", lambda dist=None: {"ok": False, "path": "d", "problems": ["index.html is missing"]})
    said.clear()
    assert bootstrap.ensure(ok, install=True, out=said.append) == 1 and any("Node.js is only needed by developers" in s for s in said)
    monkeypatch.setattr(bootstrap, "check_frontend", lambda dist=None: {"ok": True, "path": "d", "problems": []})
    assert bootstrap.ensure(ok, out=said.append) == 0


def test_the_frontend_check_finds_a_missing_asset(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/assets/app.js"></script><link href="/assets/app.css">')
    (dist / "assets" / "app.js").write_text("x")
    res = bootstrap.check_frontend(dist)
    assert res["ok"] is False and res["problems"] == ["/assets/app.css is referenced by index.html but missing"]
    assert bootstrap.check_frontend(tmp_path / "none")["ok"] is False


def test_launcher_creates_the_virtual_environment_and_refuses_a_broken_or_old_one(tmp_path, capsys):
    from stockdata.web import launcher
    venv = tmp_path / ".venv"
    exe = launcher.ensure_venv(venv)                                   # first start: created
    assert exe is not None and exe.exists() and "creating the virtual environment" in capsys.readouterr().out
    assert launcher.ensure_venv(venv) == exe and "creating" not in capsys.readouterr().out      # later starts: reused
    broken = tmp_path / "broken" / launcher.venv_python(Path("x")).relative_to("x")
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    assert launcher.ensure_venv(tmp_path / "broken") is None and "unusable or older" in capsys.readouterr().out
    old = tmp_path / "old" / launcher.venv_python(Path("x")).relative_to("x")
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\necho 3 8\n")
    old.chmod(0o755)
    assert launcher.ensure_venv(tmp_path / "old") is None


def test_launcher_stops_with_a_clear_message_on_an_old_python(monkeypatch, capsys):
    from stockdata.web import launcher
    monkeypatch.setattr(launcher.sys, "version_info", (3, 8, 0, "final", 0))
    assert launcher.main(["--check-only"]) == 1
    assert "3.10 or newer is required" in capsys.readouterr().out


@pytest.mark.skipif(not os.environ.get("STOCKDATA_TEST_FIRST_RUN"), reason="downloads packages (minutes, needs internet); set STOCKDATA_TEST_FIRST_RUN=1")
def test_first_run_from_nothing_creates_the_venv_installs_the_packages_and_starts(tmp_path, monkeypatch):
    from stockdata.web import launcher
    copy = tmp_path / "project"
    import shutil
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".venv", "node_modules", "data", ".git", "__pycache__", ".pytest_cache"))
    port = free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("STOCKDATA_DIR",)}
    env.update(STOCKDATA_ENV_FILE=str(tmp_path / "e.env"))
    r = subprocess.run([PY, "-m", "stockdata.web.launcher", "--no-browser", "--port", str(port)], cwd=copy, env=env, capture_output=True, text=True, timeout=1500)
    try:
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        assert "creating the virtual environment" in r.stdout and "Installing the required Python packages" in r.stdout and "Running at" in r.stdout
        assert (copy / ".venv").exists() and (copy / "data" / "web" / "app_control.sqlite").exists()
    finally:
        subprocess.run([str(launcher.venv_python(copy / ".venv")), "-m", "stockdata.web", "stop", "--keep-downloads"], cwd=copy, env=env, capture_output=True, timeout=60)
