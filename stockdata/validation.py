"""Validation of a historical-candles response before anything is written.

Hard failures (``ValidationError``) mean the response cannot be trusted as a
whole: the window is recorded as failed and *nothing* is inserted.
Soft findings (odd but possibly real, e.g. a special Saturday session) are
kept in the candle table and listed in ``anomalies`` so nothing is silently
discarded. Zero-volume candles are valid data and are only counted.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockdata.timeframes import Timeframe
from stockdata.timeutil import epoch_to_ist_naive
from stockdata.windows import Window

REQUIRED_KEYS = ("open", "high", "low", "close", "volume", "timestamp")
PRICE_COLS = ("open", "high", "low", "close")
SESSION_OPEN_MIN = 9 * 60 + 15
SESSION_CLOSE_MIN = 15 * 60 + 30

# If more than this share of rows is flagged (or most rows of a tiny response),
# the problem is systematic (wrong epoch base, wrong tz, swapped columns), not data noise.
SYSTEMATIC_FRACTION = 0.05
SYSTEMATIC_MIN_ROWS = 20
SYSTEMATIC_TINY_FRACTION = 0.5
MAX_CONFLICTING_KEYS = 5        # more candle keys than this with DIFFERENT candles in one response: reject it all
# Informational findings (a genuine special weekend session) must never make a window "systematic".
SYSTEMATIC_KINDS = {"ohlc_relation", "non_positive_price", "negative_volume", "outside_session", "off_grid",
                    "daily_timestamp_not_midnight_ist"}


class ValidationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class Validated:
    candles: pd.DataFrame      # key_col, open, high, low, close, volume, epoch_raw
    anomalies: pd.DataFrame    # candle_key, kind, detail
    zero_volume_rows: int = 0

    @property
    def n(self) -> int:
        return len(self.candles)


def empty_result(tf: Timeframe) -> Validated:
    cols = [tf.key_col, *PRICE_COLS, "volume", "epoch_raw"]
    return Validated(pd.DataFrame({c: [] for c in cols}),
                     pd.DataFrame({"candle_key": pd.Series([], dtype="datetime64[us]"),
                                   "kind": pd.Series([], dtype="object"),
                                   "detail": pd.Series([], dtype="object")}))


def _as_integers(values: pd.Series, name: str, *, non_negative: bool = False) -> pd.Series:
    num = pd.to_numeric(values, errors="coerce")
    if num.isna().any() or not np.isfinite(num.to_numpy(dtype="float64")).all():
        raise ValidationError("non_numeric_" + name, f"{name} contains null or non-numeric values")
    if (num != np.floor(num)).any():
        raise ValidationError("non_integer_" + name, f"{name} contains non-integral values")
    if non_negative and (num < 0).any():
        raise ValidationError("negative_" + name, f"{name} contains negative values")
    return num.astype("int64")


def validate_response(tf: Timeframe, body, window: Window) -> Validated:
    if not isinstance(body, dict) or any(k not in body for k in REQUIRED_KEYS):
        present = sorted(body) if isinstance(body, dict) else type(body).__name__
        raise ValidationError("malformed_response", f"required arrays missing; got {present}")
    arrays = {k: body[k] for k in REQUIRED_KEYS}
    if any(not isinstance(v, list) for v in arrays.values()):
        raise ValidationError("malformed_response", "candle fields are not arrays")
    lengths = {k: len(v) for k, v in arrays.items()}
    if len(set(lengths.values())) != 1:
        raise ValidationError("length_mismatch", f"array lengths differ: {lengths}")
    if lengths["timestamp"] == 0:
        return empty_result(tf)  # well-formed and empty: a successfully empty window

    df = pd.DataFrame(arrays)
    for col in PRICE_COLS:
        s = pd.to_numeric(df[col], errors="coerce")
        if s.isna().any() or not np.isfinite(s.to_numpy(dtype="float64")).all():
            raise ValidationError("non_numeric_price", f"{col} contains null or non-numeric values")
        df[col] = s.astype("float64")
    df["volume"] = _as_integers(df["volume"], "volume")      # negatives are kept and flagged, see _find_anomalies
    df["epoch_raw"] = _as_integers(df["timestamp"], "timestamp")
    try:
        ist = epoch_to_ist_naive(df["epoch_raw"])
    except (OverflowError, ValueError) as exc:  # e.g. milliseconds read as seconds
        raise ValidationError("timestamp_conversion", f"epoch values not convertible: {exc}") from None

    if tf.is_intraday:
        key = ist
    else:
        key = ist.dt.normalize()
    df, ist, key, duplicate_notes = _resolve_duplicates(df, ist, key)
    outside = (key < window.start) | (key >= window.end)
    if outside.any():
        raise ValidationError(
            "out_of_window",
            f"{int(outside.sum())} of {len(df)} candles fall outside requested "
            f"[{window.start}, {window.end}); first={key[outside].min()} last={key[outside].max()}",
        )

    anomalies = _find_anomalies(tf, df, ist, key)
    if len(duplicate_notes):
        anomalies = pd.concat([anomalies, duplicate_notes], ignore_index=True)
        anomalies["candle_key"] = anomalies["candle_key"].astype("datetime64[us]")
    flagged = anomalies.loc[anomalies["kind"].isin(SYSTEMATIC_KINDS), "candle_key"].nunique()
    n = len(df)
    systematic = (n >= SYSTEMATIC_MIN_ROWS and flagged / n > SYSTEMATIC_FRACTION) or \
                 (n < SYSTEMATIC_MIN_ROWS and flagged / n > SYSTEMATIC_TINY_FRACTION)
    if systematic:
        kinds = anomalies["kind"].value_counts().to_dict()
        raise ValidationError("systematic_anomalies",
                              f"{flagged}/{n} candles flagged {kinds}; likely a time-base or field problem")

    candles = pd.DataFrame({
        tf.key_col: key, "open": df["open"], "high": df["high"], "low": df["low"],
        "close": df["close"], "volume": df["volume"], "epoch_raw": df["epoch_raw"],
    }).sort_values(tf.key_col, kind="stable").reset_index(drop=True)
    return Validated(candles, anomalies, int((df["volume"] == 0).sum()))


def _resolve_duplicates(df: pd.DataFrame, ist: pd.Series, key: pd.Series):
    """One response can carry the same candle key more than once (seen live: WELSPUNLIV, daily, 1990 to today).

    * IDENTICAL copies lose no information: one is kept and the repeat is flagged ``duplicate_dropped``.
    * DIFFERENT candles for one key cannot be told apart (a special session, a data glitch): none of them is stored, nothing is
      merged or guessed, each raw row is written into the flag ``duplicate_conflict`` so a person can decide, and the date shows
      up as a visible gap. The rest of the response is stored normally.
    * Many conflicting keys look like a time-base problem, so the whole response is rejected as before."""
    dup = key.duplicated(keep=False)
    if not dup.any():
        return df, ist, key, pd.DataFrame({"candle_key": pd.Series([], dtype="datetime64[us]"), "kind": [], "detail": []})
    keep = pd.Series(True, index=df.index)
    notes: list[tuple] = []
    conflicts: list = []
    for k, grp in df[dup].groupby(key[dup], sort=True):
        if len(grp[list(PRICE_COLS) + ["volume"]].drop_duplicates()) == 1:
            keep[grp.index[1:]] = False
            notes.append((k, "duplicate_dropped", f"{len(grp)} identical copies of this candle in one response; one kept"))
        else:
            keep[grp.index] = False
            conflicts.append(k)
            rows = "; ".join(f"epoch {int(r.epoch_raw)} o={r.open:g} h={r.high:g} l={r.low:g} c={r.close:g} v={int(r.volume)}"
                             for r in grp.itertuples())
            notes.append((k, "duplicate_conflict", f"{len(grp)} different candles for one key, none stored: {rows}"[:480]))
    distinct = key.nunique()
    if len(conflicts) > MAX_CONFLICTING_KEYS or len(conflicts) * 2 > distinct:
        pairs = [g for _, g in df[dup].groupby(key[dup], sort=True) if len(g[list(PRICE_COLS) + ["volume"]].drop_duplicates()) > 1]
        ratio = [float(g["close"].iloc[1] / g["close"].iloc[0]) for g in pairs if g["close"].iloc[0] > 0]
        shape = f"; second/first close ratio median {sorted(ratio)[len(ratio) // 2]:.4g}" if ratio else ""
        raise ValidationError("duplicate_timestamps",
                              f"{len(conflicts)} dates carry different candles in one response (from {str(conflicts[0])[:10]} to "
                              f"{str(conflicts[-1])[:10]}{shape}): the response seems to hold overlapping series, so nothing was stored. "
                              "Run `python -m stockdata diagnose` for this instrument to see what the provider returned")
    out = pd.DataFrame(notes, columns=["candle_key", "kind", "detail"])
    return (df[keep].reset_index(drop=True), ist[keep].reset_index(drop=True), key[keep].reset_index(drop=True), out)


def _find_anomalies(tf: Timeframe, df: pd.DataFrame, ist: pd.Series, key: pd.Series) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []

    def flag(mask: pd.Series, kind: str, detail) -> None:
        if mask.any():
            d = detail[mask] if isinstance(detail, pd.Series) else detail
            rows.append(pd.DataFrame({"candle_key": key[mask].to_numpy(), "kind": kind,
                                      "detail": d.to_numpy() if isinstance(d, pd.Series) else d}))

    o, h, l, c = (df[x] for x in PRICE_COLS)
    flag((h < np.maximum(o, c)) | (l > np.minimum(o, c)) | (h < l), "ohlc_relation",
         "o=" + o.astype(str) + " h=" + h.astype(str) + " l=" + l.astype(str) + " c=" + c.astype(str))
    flag((df[list(PRICE_COLS)] <= 0).any(axis=1), "non_positive_price", "price <= 0")
    # Seen live (RELIANCE/TCS/INFY, Saturday 2022-04-09): a bar with negative volume. Stored as received.
    flag(df["volume"] < 0, "negative_volume", "volume " + df["volume"].astype(str))
    if tf.is_intraday:
        mod = ist.dt.hour * 60 + ist.dt.minute
        inside = (mod >= SESSION_OPEN_MIN) & (mod < SESSION_CLOSE_MIN)
        # Seen live: many sessions carry a bar starting 15:30 (closing/post-close trades). It is real data and
        # informational, unlike a bar far outside the session (a time-base problem), so it is a separate kind.
        post_close = (mod >= SESSION_CLOSE_MIN) & (mod < SESSION_CLOSE_MIN + 30)
        flag(post_close, "post_close_bar", "bar start " + ist.dt.strftime("%H:%M"))
        flag(~inside & ~post_close, "outside_session", "bar start " + ist.dt.strftime("%H:%M"))
        flag(inside & (((mod - SESSION_OPEN_MIN) % tf.interval != 0) | (ist.dt.second != 0)),
             "off_grid", "bar start " + ist.dt.strftime("%H:%M:%S"))
        flag(key.dt.dayofweek >= 5, "weekend_candle", "date " + key.dt.strftime("%Y-%m-%d"))
    else:
        flag(ist != key, "daily_timestamp_not_midnight_ist", "time " + ist.dt.strftime("%H:%M:%S"))
        flag(key.dt.dayofweek >= 5, "weekend_candle", "date " + key.dt.strftime("%Y-%m-%d"))
    if not rows:
        return pd.DataFrame({"candle_key": pd.Series([], dtype="datetime64[us]"),
                             "kind": [], "detail": []})
    out = pd.concat(rows, ignore_index=True)
    out["candle_key"] = out["candle_key"].astype("datetime64[us]")
    return out
