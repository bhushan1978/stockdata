"""Where the web application keeps things, and how the data directory is chosen.

The data directory is decided ONCE at launch (always resolved to an absolute path) and then used for the databases,
the shared request ledger, the control database, job folders and logs. Precedence:
``--data-dir`` > ``STOCKDATA_DIR`` > ``stockdata_web.json`` in the repository root (written by the Settings screen,
applied on the next start) > ``<repository>/data``.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from stockdata import __version__
from stockdata.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_FILE = "stockdata_web.json"
DEFAULT_PORT = 8765


def read_bootstrap(repo_root: Path = REPO_ROOT) -> dict:
    try:
        return json.loads((repo_root / BOOTSTRAP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_bootstrap(data: dict, repo_root: Path = REPO_ROOT) -> None:
    path = repo_root / BOOTSTRAP_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def resolve_data_dir(explicit: str | None, repo_root: Path = REPO_ROOT) -> tuple[Path, str]:
    if explicit:
        return Path(explicit).expanduser().resolve(), "--data-dir"
    if os.environ.get("STOCKDATA_DIR"):
        return Path(os.environ["STOCKDATA_DIR"]).expanduser().resolve(), "STOCKDATA_DIR"
    boot = read_bootstrap(repo_root).get("data_dir")
    if boot:
        return Path(boot).expanduser().resolve(), BOOTSTRAP_FILE
    return (repo_root / "data").resolve(), "default (<repository>/data)"


def git_commit(repo_root: Path = REPO_ROOT) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, capture_output=True,
                             text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True,
                               timeout=5).stdout.strip()
        return (out.stdout.strip() or "unknown") + ("+local changes" if dirty else "")
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class WebSettings:
    data_dir: Path
    data_dir_source: str = "default"
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    repo_root: Path = REPO_ROOT
    static_dir: Path | None = None
    env_file: Path | None = None
    dev_origins: tuple[str, ...] = ()
    extra_hosts: tuple[str, ...] = ()           # tests only
    engine: Settings = field(default=None)       # the downloader's own settings (ledger path, caps, rps, token source)
    version: str = __version__
    commit: str = "unknown"

    # paths ---------------------------------------------------------------------------------------------------
    @property
    def web_dir(self) -> Path: return self.data_dir / "web"
    @property
    def control_db(self) -> Path: return self.web_dir / "app_control.sqlite"
    @property
    def jobs_dir(self) -> Path: return self.web_dir / "jobs"
    @property
    def log_dir(self) -> Path: return self.web_dir / "logs"
    @property
    def server_info(self) -> Path: return self.web_dir / "server.json"
    @property
    def server_lock(self) -> Path: return self.web_dir / "server.lock"
    @property
    def reports_dir(self) -> Path: return self.data_dir / "reports"
    @property
    def credentials_file(self) -> Path: return self.env_file or (self.repo_root / ".env")

    @classmethod
    def load(cls, *, data_dir: str | None = None, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
             dev: bool = False, repo_root: Path = REPO_ROOT, static_dir: Path | None = None) -> "WebSettings":
        resolved, source = resolve_data_dir(data_dir, repo_root)
        engine = Settings.from_env(str(resolved))
        dist = static_dir if static_dir is not None else repo_root / "webui" / "dist"
        env_file = Path(os.environ["STOCKDATA_ENV_FILE"]).expanduser().resolve() if os.environ.get("STOCKDATA_ENV_FILE") else None
        origins = ("http://localhost:5173", "http://127.0.0.1:5173") if dev else ()
        return cls(data_dir=resolved, data_dir_source=source, host=host, port=port, repo_root=repo_root,
                   static_dir=dist if (dist / "index.html").exists() else None, engine=engine,
                   dev_origins=origins, commit=git_commit(repo_root), env_file=env_file)
