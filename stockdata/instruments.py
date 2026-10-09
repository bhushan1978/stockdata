"""Dhan instrument master: parsing, universe selection, summary.

The master CSV layout is *not assumed*: columns are matched by alias, the
parser fails loudly (listing the columns it found) if a required one is
missing, and ``summary`` prints the distinct values so the universe filter can
be checked against the real file before any download.
"""
from __future__ import annotations

import hashlib
import io
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# canonical name -> accepted header names (compact master first, detailed second).
# These aliases come from the published master layouts as recalled and are
# UNVERIFIED against the live file; `instruments summary` shows what is really there.
ALIASES: dict[str, tuple[str, ...]] = {
    "exchange_id": ("SEM_EXM_EXCH_ID", "EXCH_ID"),
    "segment_code": ("SEM_SEGMENT", "SEGMENT"),
    "security_id": ("SEM_SMST_SECURITY_ID", "SECURITY_ID"),
    "instrument": ("SEM_INSTRUMENT_NAME", "INSTRUMENT"),
    "instrument_type": ("SEM_EXCH_INSTRUMENT_TYPE", "INSTRUMENT_TYPE"),
    "series": ("SEM_SERIES", "SERIES"),
    "symbol": ("SEM_TRADING_SYMBOL", "UNDERLYING_SYMBOL", "TRADING_SYMBOL"),
    "name": ("SM_SYMBOL_NAME", "SYMBOL_NAME", "SEM_CUSTOM_SYMBOL", "DISPLAY_NAME"),
    "isin": ("ISIN", "SEM_ISIN"),
    "lot_size": ("SEM_LOT_UNITS", "LOT_SIZE"),
    "tick_size": ("SEM_TICK_SIZE", "TICK_SIZE"),
}
REQUIRED = ("exchange_id", "segment_code", "security_id", "instrument")

# (exchange_id, segment_code) -> API exchangeSegment. Equity only on purpose.
API_SEGMENTS = {("NSE", "E"): "NSE_EQ", ("BSE", "E"): "BSE_EQ"}

CANONICAL_COLUMNS = list(ALIASES)


class MasterFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Instrument:
    exchange_segment: str
    security_id: int
    symbol: str
    instrument: str
    name: str = ""
    isin: str = ""
    series: str = ""


@dataclass(frozen=True)
class UniverseSpec:
    """Filter over the instrument master. Defaults: NSE cash equities, series EQ."""
    exchange_id: str = "NSE"
    segment_code: str = "E"
    instrument: str = "EQUITY"
    series: tuple[str, ...] = ("EQ",)
    instrument_types: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_toml(cls, path: str | Path) -> "UniverseSpec":
        data = tomllib.loads(Path(path).read_text())
        return cls(
            exchange_id=data.get("exchange_id", "NSE"),
            segment_code=data.get("segment_code", "E"),
            instrument=data.get("instrument", "EQUITY"),
            series=tuple(data.get("series", ["EQ"])),
            instrument_types=tuple(data.get("instrument_types", [])),
        )


def read_master(source: str | Path | bytes) -> pd.DataFrame:
    """Read a master CSV (path or bytes) as strings, unmodified."""
    buf = io.BytesIO(source) if isinstance(source, bytes) else source
    return pd.read_csv(buf, dtype=str, keep_default_na=False, low_memory=False)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_master(raw: pd.DataFrame) -> pd.DataFrame:
    """Map a raw master onto canonical columns; strip and upper-case keys."""
    cols = {c.strip().upper(): c for c in raw.columns}
    out = pd.DataFrame(index=raw.index)
    missing = []
    for canon, names in ALIASES.items():
        src = next((cols[n] for n in names if n in cols), None)
        if src is None:
            if canon in REQUIRED:
                missing.append(f"{canon} (one of {', '.join(names)})")
            out[canon] = ""
        else:
            out[canon] = raw[src].astype(str).str.strip()
    if missing:
        raise MasterFormatError(
            "instrument master is missing required columns: " + "; ".join(missing)
            + f". Columns found: {', '.join(raw.columns)}"
        )
    for c in ("exchange_id", "segment_code", "instrument", "series", "instrument_type"):
        out[c] = out[c].str.upper()
    bad = ~out["security_id"].str.fullmatch(r"\d+")
    if bad.any():
        raise MasterFormatError(
            f"{int(bad.sum())} rows have a non-numeric security id, e.g. "
            f"{out.loc[bad, 'security_id'].head(3).tolist()}"
        )
    out["security_id"] = out["security_id"].astype("int64")
    return out


def select_universe(master: pd.DataFrame, spec: UniverseSpec) -> pd.DataFrame:
    """Rows of a *normalized* master matching ``spec``, with ``exchange_segment`` added."""
    m = master
    mask = (
        (m["exchange_id"] == spec.exchange_id.upper())
        & (m["segment_code"] == spec.segment_code.upper())
        & (m["instrument"] == spec.instrument.upper())
    )
    if spec.series:
        mask &= m["series"].isin([s.upper() for s in spec.series])
    if spec.instrument_types:
        mask &= m["instrument_type"].isin([s.upper() for s in spec.instrument_types])
    key = (spec.exchange_id.upper(), spec.segment_code.upper())
    if key not in API_SEGMENTS:
        raise MasterFormatError(
            f"no API exchangeSegment mapping for exchange/segment {key}; "
            f"supported: {sorted(API_SEGMENTS)}"
        )
    sel = m.loc[mask].copy()
    sel["exchange_segment"] = API_SEGMENTS[key]
    dup = sel.duplicated(["exchange_segment", "security_id"], keep=False)
    if dup.any():
        raise MasterFormatError(
            f"{int(dup.sum())} universe rows share a security id; tighten the universe filter"
        )
    return sel.reset_index(drop=True)


def summarize(master: pd.DataFrame, limit: int = 40) -> str:
    """Distinct (exchange, segment, instrument, type, series) combinations and sample rows."""
    keys = ["exchange_id", "segment_code", "instrument", "instrument_type", "series"]
    counts = master.groupby(keys, dropna=False).size().sort_values(ascending=False)
    lines = [f"{len(master):,} rows; combinations of {', '.join(keys)}:"]
    for idx, n in counts.head(limit).items():
        lines.append(f"  {n:>7,}  " + " | ".join(str(v) or "-" for v in idx))
    if len(counts) > limit:
        lines.append(f"  ... {len(counts) - limit} more combinations")
    return "\n".join(lines)
