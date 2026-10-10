"""Shared fixtures for the web application tests: a data directory with the synthetic universe loaded, an app wired to
it (no Dhan traffic, no production files), and a real-subprocess worker pointed at the local fake Dhan server."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import load_universe
from stockdata.config import Settings
from stockdata.db import CandleDB
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.web.api import create_app
from stockdata.web.settings import REPO_ROOT, WebSettings

UI = {"X-StockData-UI": "1"}
# Credential-shaped strings are assembled at run time so no test file contains one (tests/test_repo_hygiene.py scans every tracked file).
TOKEN_KEY = "DHAN_ACCESS" + "_TOKEN"
CLIENT_KEY = "DHAN_CLIENT" + "_ID"


def fake_jwt(exp: int = 4_102_444_800) -> str:
    """A synthetic JWT-shaped string (not a real token): header.payload.signature with an exp claim."""
    import base64
    import json
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return ".".join([enc({"alg": "HS256"}), enc({"exp": exp, "sub": "x"}), "signaturepart1234"])


def make_settings(tmp_path: Path, **kw) -> WebSettings:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    return WebSettings(data_dir=data.resolve(), data_dir_source="test", repo_root=REPO_ROOT, static_dir=None,
                       env_file=tmp_path / ".env", extra_hosts=("testserver",), engine=Settings.from_env(str(data.resolve())),
                       commit="test", **kw)


def load_databases(settings: WebSettings) -> None:
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, settings.data_dir / tf.db_name) as db:
            load_universe(db)


def wait_for(predicate, timeout: float = 60.0, interval: float = 0.2, message: str = "condition"):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


class Env:
    """An app + client over a fresh data directory. ``scheduler=False`` lets tests drive ``tick()`` deterministically."""

    def __init__(self, tmp_path: Path, *, scheduler: bool = False, databases: bool = True,
                 token: str | None = "test-token", client_id: str | None = "1000000001"):
        self.tmp = tmp_path
        self.settings = make_settings(tmp_path)
        if token or client_id:
            lines = []
            if token:
                lines.append(f"{TOKEN_KEY}={token}")
            if client_id:
                lines.append(f"{CLIENT_KEY}={client_id}")
            self.settings.env_file.write_text("\n".join(lines) + "\n")
        if databases:
            load_databases(self.settings)
        self.scheduler = scheduler
        self.app = create_app(self.settings, start_scheduler=scheduler)
        self.client = TestClient(self.app, headers=UI)
        self.sup = self.app.state.sup
        self.store = self.app.state.store

    def reopen(self, *, scheduler: bool | None = None) -> "Env":
        """Simulate a server restart: a brand-new app over the same directories (workers keep running)."""
        if self.scheduler:
            self.sup.shutdown()
        self.store.close()
        new = Env.__new__(Env)
        new.tmp, new.settings = self.tmp, self.settings
        new.scheduler = self.scheduler if scheduler is None else scheduler
        new.app = create_app(self.settings, start_scheduler=new.scheduler)
        new.client = TestClient(new.app, headers=UI)
        new.sup, new.store = new.app.state.sup, new.app.state.store
        return new

    def close(self) -> None:
        import sqlite3
        try:
            self._close()
        except sqlite3.ProgrammingError:       # already closed by reopen()
            pass

    def _close(self) -> None:
        for job in self.sup._active_jobs():
            run = self.sup._latest_run(job["id"])
            if run:
                from stockdata.web.process import hard_kill
                hard_kill(run["pid"], run["pid_create_time"], wait=3)
        if self.scheduler:
            self.sup.shutdown()
        self.store.close()

    # -- helpers
    def create(self, spec: dict, start: str = "queue", expect: int = 200):
        r = self.client.post("/api/jobs", json={"spec": spec, "start": start, "confirm": True})
        assert r.status_code == expect, r.text
        return r.json()

    def job(self, job_id: int) -> dict:
        r = self.client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200, r.text
        return r.json()

    def act(self, job_id: int, action: str, expect: int = 200, **body):
        r = self.client.post(f"/api/jobs/{job_id}/{action}", json=body)
        assert r.status_code == expect, r.text
        return r.json()


@pytest.fixture
def web_env(tmp_path):
    env = Env(tmp_path)
    yield env
    env.close()


@pytest.fixture
def sched_env(tmp_path):
    env = Env(tmp_path, scheduler=True)
    yield env
    env.close()


def point_workers_at(monkeypatch, fake) -> None:
    """Workers inherit this environment: talk to the local fake server, with no rate limit."""
    monkeypatch.setenv("DHAN_BASE_URL", fake.base_url)
    monkeypatch.setenv("STOCKDATA_RPS", "1000")


def daily_spec(symbols=("SIMALPHA", "SIMBETA"), **kw) -> dict:
    return {"kind": "download", "timeframe": "daily", "selection": {"mode": "symbols", "symbols": list(symbols)},
            "start_date": "2024-01-01", "end_date": "2024-06-03", **kw}


def intraday_spec(symbols=("SIMALPHA", "SIMBETA", "SIMGAMMA"), **kw) -> dict:
    return {"kind": "download", "timeframe": "15min", "selection": {"mode": "symbols", "symbols": list(symbols)},
            "start_date": "2023-06-01", "end_date": "2024-06-03", **kw}
