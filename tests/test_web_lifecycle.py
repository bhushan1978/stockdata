"""The real server process: single instance per data directory, local-only binding, clean stop that leaves download
workers alone, and re-adoption of a running worker by the next server."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import pytest

from conftest import load_universe
from stockdata.db import CandleDB
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.web import lifecycle
from web_helpers import intraday_spec, wait_for

REPO = Path(__file__).parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(port, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-StockData-UI": "1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Server:
    def __init__(self, data, port, env):
        self.data, self.port = data, port
        self.proc = subprocess.Popen([sys.executable, "-m", "stockdata.web", "--data-dir", str(data), "serve", "--port", str(port)],
                                     cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        wait_for(lambda: self._up(), timeout=30, message="server start")

    def _up(self):
        if self.proc.poll() is not None:
            raise AssertionError("server exited early:\n" + self.proc.stdout.read())
        try:
            return http(self.port, "GET", "/api/health")[0] == 200
        except OSError:
            return False

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "stockdata.web", "--data-dir", str(self.data), *args], cwd=REPO,
                              capture_output=True, text=True, timeout=90)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()


@pytest.fixture
def server_env(tmp_path, fake):
    data = tmp_path / "data"
    data.mkdir()
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, data / tf.db_name) as db:
            load_universe(db)
    env = {**os.environ, "DHAN_ACCESS_TOKEN": "test-token", "DHAN_CLIENT_ID": "1000000001", "DHAN_BASE_URL": fake.base_url,
           "STOCKDATA_RPS": "1000", "STOCKDATA_DIR": ""}
    env.pop("STOCKDATA_DIR")
    servers = []
    yield data, env, servers
    for s in servers:
        s.kill()
    time.sleep(0.2)
    for p in psutil.process_iter(["cmdline"]):                       # leave no worker behind
        try:
            if "stockdata.web.worker" in " ".join(p.info["cmdline"] or []) and str(data) in " ".join(p.info["cmdline"]):
                p.kill()
        except psutil.Error:
            pass


def test_server_lock_is_exclusive_and_released_with_the_process(tmp_path):
    a, b = lifecycle.ServerLock(tmp_path / "x.lock"), lifecycle.ServerLock(tmp_path / "x.lock")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_one_server_per_data_dir_loopback_only_and_a_busy_port_is_explained(server_env):
    data, env, servers = server_env
    port = free_port()
    s = Server(data, port, env)
    servers.append(s)
    status, health = http(port, "GET", "/api/health")
    assert status == 200 and health["bind"] == f"127.0.0.1:{port}" and health["scheduler_alive"] is True
    second = subprocess.run([sys.executable, "-m", "stockdata.web", "--data-dir", str(data), "serve", "--port", str(free_port())],
                            cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert second.returncode == 3 and "already uses this data directory" in second.stderr
    remote = subprocess.run([sys.executable, "-m", "stockdata.web", "--data-dir", str(data), "serve", "--host", "0.0.0.0"], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=60)
    assert remote.returncode == 2 and "non-loopback" in remote.stderr
    out = s.cli("status", "--port", str(port))
    assert "server: running" in out.stdout and "none active" in out.stdout
    stop = s.cli("stop", "--port", str(port))
    assert stop.returncode == 0 and "No download was running" in stop.stdout
    wait_for(lambda: s.proc.poll() is not None, timeout=30, message="server exit")
    assert lifecycle.read_info(data / "web" / "server.json") is None                   # info file removed
    other = free_port()
    busy = socket.socket(); busy.bind(("127.0.0.1", other)); busy.listen(1)
    try:
        r = subprocess.run([sys.executable, "-m", "stockdata.web", "--data-dir", str(data), "serve", "--port", str(other)], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 4 and "already in use" in r.stderr and "netstat" in r.stderr
    finally:
        busy.close()


def test_stopping_the_server_leaves_a_running_download_alone_and_the_next_server_readopts_it(server_env, fake):
    data, env, servers = server_env
    fake.delay = 0.4
    port = free_port()
    s = Server(data, port, env)
    servers.append(s)
    code, made = http(port, "POST", "/api/jobs", {"spec": intraday_spec(), "start": "now", "confirm": True})
    assert code == 200 and made["started"] is True
    job_id = made["job"]["id"]
    wait_for(lambda: http(port, "GET", f"/api/jobs/{job_id}")[1]["progress"]["run_counts"].get("windows_done", 0) >= 1, timeout=60)
    pid = http(port, "GET", f"/api/jobs/{job_id}")[1]["worker"]["pid"]
    ask = s.cli("stop", "--port", str(port))
    assert ask.returncode == 2 and "--pause-downloads or --keep-downloads" in ask.stderr and s.proc.poll() is None    # no silent choice for you
    stop = s.cli("stop", "--port", str(port), "--keep-downloads")
    assert "STILL RUNNING" in stop.stdout.upper() and stop.returncode == 0
    wait_for(lambda: s.proc.poll() is not None, timeout=30, message="server exit")
    assert psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE            # the download did not die with the server
    status = s.cli("status", "--port", str(port))
    assert "server: not running" in status.stdout and "ALIVE" in status.stdout                        # visible even with the server down
    port2 = free_port()
    s2 = Server(data, port2, env)
    servers.append(s2)
    j = http(port2, "GET", f"/api/jobs/{job_id}")[1]
    assert j["worker"]["pid"] == pid and j["worker"]["alive"] and j["status"] in ("running", "preparing")      # re-adopted, no second worker
    assert len([p for p in psutil.process_iter(["cmdline"]) if "stockdata.web.worker" in " ".join(p.info["cmdline"] or []) and str(data) in " ".join(p.info["cmdline"])]) == 1
    fake.delay = 0
    done = wait_for(lambda: http(port2, "GET", f"/api/jobs/{job_id}")[1]["status"] == "completed" or None, timeout=90, message="job completes under the new server")
    stop2 = s2.cli("stop", "--port", str(port2), "--pause-downloads")
    assert stop2.returncode == 0
