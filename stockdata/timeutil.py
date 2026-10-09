"""Explicit, single-place time handling.

All candle times are stored as *naive wall-clock Asia/Kolkata* values. Dhan
returns Unix epoch seconds; they are converted UTC -> Asia/Kolkata here and
nowhere else. The raw epoch is kept next to every candle for audit.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (stored columns are *_utc)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ist_now() -> datetime:
    return datetime.now(IST)


def epoch_to_ist_naive(epochs: pd.Series) -> pd.Series:
    """Unix epoch seconds -> naive Asia/Kolkata datetimes (microsecond resolution)."""
    ts = pd.to_datetime(epochs.astype("int64"), unit="s", utc=True)
    return ts.dt.tz_convert(IST).dt.tz_localize(None).astype("datetime64[us]")


def cutoff_exclusive(now_ist: datetime, finalize_after_hour: int) -> datetime:
    """Exclusive upper bound for requests: midnight IST of the first day whose
    candles are not yet final. Before the finalize hour, today is excluded."""
    today = datetime.combine(now_ist.date(), time.min)
    return today + timedelta(days=1) if now_ist.hour >= finalize_after_hour else today


def format_request_date(dt: datetime, kind: str) -> str:
    if kind == "date":
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def to_midnight(d: date | datetime) -> datetime:
    return datetime.combine(d.date() if isinstance(d, datetime) else d, time.min)
