"""Request-window planning.

Coverage of an instrument is the union of its *complete* and *empty* windows.
Missing ranges are split into windows of at most ``max_days`` days, newest
first. Chunking is anchored at the end of each missing range, so a retry of
the same gap yields the same window keys (needed for resumable checkpoints).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, order=True)
class Window:
    start: datetime
    end: datetime  # exclusive

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Union of half-open intervals; touching intervals are merged."""
    out: list[tuple[datetime, datetime]] = []
    for a, b in sorted(i for i in intervals if i[0] < i[1]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def subtract(covered: list[tuple[datetime, datetime]], lo: datetime, hi: datetime
             ) -> list[tuple[datetime, datetime]]:
    """Parts of ``[lo, hi)`` not covered by the (merged) intervals."""
    gaps, cur = [], lo
    for a, b in covered:
        if b <= cur:
            continue
        if a >= hi:
            break
        if a > cur:
            gaps.append((cur, min(a, hi)))
        cur = max(cur, b)
    if cur < hi:
        gaps.append((cur, hi))
    return gaps


def chunk_backward(a: datetime, b: datetime, max_days: int) -> list[Window]:
    """Split ``[a, b)`` into windows of <= max_days, newest first, anchored at b."""
    step, out, end = timedelta(days=max_days), [], b
    while end > a:
        start = max(a, end - step)
        out.append(Window(start, end))
        end = start
    return out


def plan_windows(covered: list[tuple[datetime, datetime]], history_start: datetime,
                 cutoff: datetime, max_days: int, overlap_days: int = 0) -> list[Window]:
    """Windows still to request to cover ``[history_start, cutoff)``, newest first.

    When data already exists below the cutoff, the forward gap starts
    ``overlap_days`` earlier so a retroactive revision of recent candles is seen.
    """
    if history_start >= cutoff:
        return []
    merged = merge_intervals(covered)
    ranges = subtract(merged, history_start, cutoff)
    if overlap_days and ranges and ranges[-1][1] == cutoff and ranges[-1][0] > history_start:
        a, b = ranges[-1]
        ranges[-1] = (max(history_start, a - timedelta(days=overlap_days)), b)
    windows = [w for a, b in ranges for w in chunk_backward(a, b, max_days)]
    return sorted(windows, key=lambda w: w.end, reverse=True)
