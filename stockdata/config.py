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

from stockdata.budget import DEFAULT_MAX_PER_DAY, DEFAULT_MAX_PER_RUN, validate_limits

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
# Verified live (verify-api, 2026-10-10): the daily endpoint accepted one 1990 -> today request
# (6,159 candles). Empty daily windows come back as an ambiguous HTTP 400 DH-907 ("incorrect parameters
# or no data present"), so use ONE window for the whole history: no pre-listing windows to misread.
DEFAULT_DAILY_WINDOW_DAYS = 40_000
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
    max_requests_per_run: int = DEFAULT_MAX_PER_RUN
    max_requests_per_day: int = DEFAULT_MAX_PER_DAY   # IST calendar day, all downloaders of this application
    ledger_path: Path | None = None                    # shared usage ledger; resolved once in from_env
    provider: str = "dhan"                             # default data provider (STOCKDATA_PROVIDER); a command's --provider wins
    rps_override: float | None = None                  # STOCKDATA_RPS when explicitly set: scales the limits of ANY provider

    @property
    def request_ledger_path(self) -> Path:
        return self.ledger_path or self.data_dir / "request_ledger.sqlite"

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
        per_run = int(env("STOCKDATA_MAX_REQUESTS_PER_RUN", DEFAULT_MAX_PER_RUN))
        per_day = int(env("STOCKDATA_MAX_REQUESTS_PER_DAY", DEFAULT_MAX_PER_DAY))
        validate_limits(per_run, per_day)
        folder = Path(data_dir or env("STOCKDATA_DIR", "data"))
        return cls(
            access_token=env("DHAN_ACCESS_TOKEN") or None,
            client_id=env("DHAN_CLIENT_ID") or None,
            base_url=env("DHAN_BASE_URL", API_BASE_URL).rstrip("/"),
            data_dir=folder,
            rps=float(env("STOCKDATA_RPS", DEFAULT_RPS)),
            intraday_date_format=fmt,
            max_requests_per_run=per_run,
            max_requests_per_day=per_day,
            ledger_path=Path(env("STOCKDATA_LEDGER") or folder / "request_ledger.sqlite"),
            provider=(env("STOCKDATA_PROVIDER") or "dhan").lower(),
            rps_override=float(env("STOCKDATA_RPS")) if env("STOCKDATA_RPS") else None,
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
