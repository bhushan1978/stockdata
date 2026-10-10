"""Market-data providers behind one interface. Dhan (existing), FYERS API v3 and Upstox all write into the SAME canonical
candle tables; only the request/response handling differs. See ``stockdata/providers/base.py``."""
from __future__ import annotations

from stockdata.providers.base import ProviderAdapter

PROVIDER_NAMES = ("dhan", "fyers", "upstox")
DEFAULT_PROVIDER = "dhan"
_CACHE: dict[str, ProviderAdapter] = {}


def get_provider(name: str) -> ProviderAdapter:
    name = (name or DEFAULT_PROVIDER).lower()
    if name not in PROVIDER_NAMES:
        raise ValueError(f"unknown provider {name!r}; choose one of {list(PROVIDER_NAMES)}")
    if name not in _CACHE:
        if name == "dhan":
            from stockdata.providers.dhan_adapter import DhanAdapter as cls
        elif name == "fyers":
            from stockdata.providers.fyers import FyersAdapter as cls
        else:
            from stockdata.providers.upstox import UpstoxAdapter as cls
        _CACHE[name] = cls()
    return _CACHE[name]


def all_providers() -> list[ProviderAdapter]:
    return [get_provider(n) for n in PROVIDER_NAMES]


def ledger_label(provider: str, timeframe: str) -> str:
    """Request-ledger label. Dhan keeps its historical bare labels ('daily', '15min') so existing usage history and the
    per-day total keep their meaning; other providers get 'provider/timeframe' and are counted against their own daily cap."""
    return timeframe if provider == "dhan" else f"{provider}/{timeframe}"


def provider_of_label(label: str) -> str:
    return label.split("/", 1)[0] if "/" in label else "dhan"
