"""The only place where daily and 15-minute downloads differ."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from stockdata import config
from stockdata.timeutil import format_request_date

if TYPE_CHECKING:  # pragma: no cover
    from stockdata.instruments import Instrument


@dataclass(frozen=True)
class Timeframe:
    name: str                 # "daily" | "15min"
    endpoint: str
    table: str
    key_col: str              # candle_date | candle_timestamp
    db_name: str
    default_window_days: int
    max_window_days: int | None
    interval: int | None      # minutes, intraday only

    @property
    def is_intraday(self) -> bool:
        return self.interval is not None

    def adjusted_view(self) -> str:
        return f"{self.table}_adjusted"

    def payload(self, inst: "Instrument", start: datetime, end_exclusive: datetime,
                *, intraday_date_format: str, client_id: str | None) -> dict:
        """Request body for one window ``[start, end_exclusive)``.

        Daily: ``toDate`` is the documented exclusive boundary, so the window end
        is sent unchanged. Intraday windows are midnight-aligned, so whether the
        API treats ``toDate`` as inclusive is immaterial: no equity bar starts at 00:00.
        """
        kind = intraday_date_format if self.is_intraday else "date"
        body = {
            "securityId": str(inst.security_id),
            "exchangeSegment": inst.exchange_segment,
            "instrument": inst.instrument,
            "oi": False,
            "fromDate": format_request_date(start, kind),
            "toDate": format_request_date(end_exclusive, kind),
        }
        if self.is_intraday:
            body["interval"] = self.interval
        else:
            body["expiryCode"] = 0
        if client_id:
            body["dhanClientId"] = client_id  # the official SDK adds this to every payload
        return body


DAILY = Timeframe(
    name="daily", endpoint="/charts/historical", table="candles_daily",
    key_col="candle_date", db_name=config.DAILY_DB_NAME,
    default_window_days=config.DEFAULT_DAILY_WINDOW_DAYS, max_window_days=None, interval=None,
)

INTRADAY_15 = Timeframe(
    name="15min", endpoint="/charts/intraday", table="candles_15min",
    key_col="candle_timestamp", db_name=config.INTRADAY_DB_NAME,
    default_window_days=config.DEFAULT_INTRADAY_WINDOW_DAYS,
    max_window_days=config.MAX_INTRADAY_WINDOW_DAYS, interval=15,
)

TIMEFRAMES = {"daily": DAILY, "15min": INTRADAY_15}


def get_timeframe(name: str) -> Timeframe:
    try:
        return TIMEFRAMES[name]
    except KeyError:
        raise ValueError(f"unknown timeframe {name!r}; choose one of {sorted(TIMEFRAMES)}") from None
