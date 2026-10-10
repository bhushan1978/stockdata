"""Settings, API constants and tunables.

Secrets (access token, client id) are read only from the environment or an
untracked ``.env`` file. They are never written to disk, logs or the databases.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

API_BASE_URL = "https://api.dhan.co/v2"
MASTER_URLS = {
    "compact": "https://images.dhan.co/api-data/api-scrip-master.csv",
    "detailed": "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
}

DAILY_DB_NAME = "stockdata_daily.duckdb"
INTRADAY_DB_NAME = "stockdata_15min.duckdb"

# Documented Historical Data limits: 5 requests/second and 100,000 requests/day (account-wide).
# Stay below the per-second limit; the daily cap is not enforced here (see README).
DEFAULT_RPS = 3.0

MAX_INTRADAY_WINDOW_DAYS = 90      # hard cap: never request more than this per call
DEFAULT_INTRADAY_WINDOW_DAYS = 89  # one day of margin under the cap
DEFAULT_DAILY_WINDOW_DAYS = 3650   # daily range limit is not documented; see verify-api
INTRADAY_HISTORY_YEARS = 5
DAILY_HISTORY_START = date(1990, 1, 1)

# Today's candles are treated as final only after this IST hour.
FINALIZE_AFTER_HOUR_IST = 18
# Re-request this many days before the covered edge on incremental runs, to
# detect retroactive revisions (e.g. a source-side corporate-action adjustment).
DEFAULT_OVERLAP_DAYS = 7


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env reader. Real environment variables always win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    access_token: str | None = field(default=None, repr=False)
    client_id: str | None = field(default=None, repr=False)
    base_url: str = API_BASE_URL
    data_dir: Path = Path("data")
    rps: float = DEFAULT_RPS
    intraday_date_format: str = "datetime"  # "datetime" | "date"

    @property
    def daily_db_path(self) -> Path:
        return self.data_dir / DAILY_DB_NAME

    @property
    def intraday_db_path(self) -> Path:
        return self.data_dir / INTRADAY_DB_NAME

    @property
    def master_dir(self) -> Path:
        return self.data_dir / "instrument_master"

    @classmethod
    def from_env(cls, data_dir: str | os.PathLike | None = None) -> "Settings":
        load_dotenv()
        env = os.environ.get
        fmt = env("DHAN_INTRADAY_DATE_FORMAT", "datetime").lower()
        if fmt not in ("datetime", "date"):
            raise ValueError("DHAN_INTRADAY_DATE_FORMAT must be 'datetime' or 'date'")
        return cls(
            access_token=env("DHAN_ACCESS_TOKEN") or None,
            client_id=env("DHAN_CLIENT_ID") or None,
            base_url=env("DHAN_BASE_URL", API_BASE_URL).rstrip("/"),
            data_dir=Path(data_dir or env("STOCKDATA_DIR", "data")),
            rps=float(env("STOCKDATA_RPS", DEFAULT_RPS)),
            intraday_date_format=fmt,
        )

    def require_token(self) -> str:
        if not self.access_token:
            raise SystemExit(
                "DHAN_ACCESS_TOKEN is not set. Put it in the environment or an untracked "
                ".env file (see .env.example). It is never stored by this tool."
            )
        host = urlparse(self.base_url)
        if host.scheme != "https" and host.hostname not in ("localhost", "127.0.0.1"):
            raise SystemExit("Refusing to send the access token over a non-HTTPS base URL.")
        return self.access_token
