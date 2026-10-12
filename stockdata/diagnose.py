"""Read-only look at what a provider actually returns for one instrument, for the cases the validator refuses.

Nothing here writes a candle: it asks for the same data a download would (a handful of counted requests), analyses the RAW response and
prints what is in it: repeated dates and how the copies relate, fractional volumes, an answer of "no data" and whether recent data exists.
The raw response is also saved under ``<data dir>/diagnostics`` (git-ignored) so it can be inspected or shared on purpose.

It exists because a refused response must be understood before any rule for storing it is invented.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from stockdata.providers.errors import AuthError, PermanentError, ProviderError, TransientError
from stockdata.timeutil import epoch_to_ist_naive
from stockdata.validation import REQUIRED_KEYS

PROBE_DAYS = 60
SAMPLE = 3
RATIO_STEP = 0.05          # the higher/lower price ratio moved by more than this: a new segment
JUMP = 1.3                 # a day-to-day move beyond +30% / -23% inside one series is reported
MAX_SEGMENTS = 8


def _iso(x) -> str | None:
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else str(x)[:19]


def analyze_body(tf, body) -> dict:
    """Facts about one raw response (arrays as the provider sent them). Never raises on odd data: it reports it."""
    if not isinstance(body, dict) or any(not isinstance(body.get(k), list) for k in REQUIRED_KEYS):
        return {"rows": 0, "problem": "the response does not hold the six candle arrays"}
    n = len(body["timestamp"])
    if any(len(body[k]) != n for k in REQUIRED_KEYS):
        return {"rows": n, "problem": "the candle arrays have different lengths"}
    if n == 0:
        return {"rows": 0}
    df = pd.DataFrame({k: body[k] for k in REQUIRED_KEYS})
    ts = pd.to_numeric(df["timestamp"], errors="coerce")
    if ts.isna().any():
        return {"rows": n, "problem": "some timestamps are not numbers"}
    try:
        ist = epoch_to_ist_naive(ts.astype("int64"))
    except (OverflowError, ValueError):
        return {"rows": n, "problem": "the timestamps cannot be read as seconds since 1970"}
    key = ist if tf.is_intraday else ist.dt.normalize()
    num = {c: pd.to_numeric(df[c], errors="coerce") for c in ("open", "high", "low", "close", "volume")}
    out: dict = {"rows": n, "unique_keys": int(key.nunique()), "first": _iso(key.min()), "last": _iso(key.max()),
                 "time_of_day": {str(k): int(v) for k, v in ist.dt.strftime("%H:%M:%S").value_counts().head(4).items()}}

    dup = key.duplicated(keep=False)
    if dup.any():
        pos = pd.Series(np.arange(n), index=df.index)
        first_pos, second_pos, identical, conflicting = [], [], 0, 0
        pairs = []
        for k, grp in df[dup].groupby(key[dup], sort=True):
            idx = list(grp.index)
            a, b = idx[0], idx[1]
            same = all(num[c][a] == num[c][b] for c in num)
            identical += int(same)
            conflicting += int(not same)
            if not same:
                pairs.append((k, a, b))
            first_pos.append(int(pos[a])); second_pos.append(int(pos[b]))
        gaps = np.array(second_pos) - np.array(first_pos)
        info = {"keys_repeated": int(key[dup].nunique()), "identical": identical,
                "conflicting": conflicting, "max_copies": int(key[dup].value_counts().max()),
                "first_key": _iso(key[dup].min()), "last_key": _iso(key[dup].max()),
                "median_row_gap_between_copies": float(np.median(gaps)), "adjacent_copies_share": round(float((gaps == 1).mean()), 3)}
        if pairs:
            c_first = np.array([num["close"][a] for _, a, _ in pairs], dtype=float)
            c_second = np.array([num["close"][b] for _, _, b in pairs], dtype=float)
            v_first = np.array([num["volume"][a] for _, a, _ in pairs], dtype=float)
            v_second = np.array([num["volume"][b] for _, _, b in pairs], dtype=float)
            ok = c_first > 0
            if ok.any():
                ratio = c_second[ok] / c_first[ok]
                med = float(np.median(ratio))
                info["close_ratio_second_over_first"] = {"median": round(med, 6), "min": round(float(ratio.min()), 6), "max": round(float(ratio.max()), 6),
                                                         "share_within_1pct_of_median": round(float((np.abs(ratio / med - 1) <= 0.01).mean()), 3)}
            okv = v_first > 0
            if okv.any():
                info["volume_ratio_second_over_first_median"] = round(float(np.median(v_second[okv] / v_first[okv])), 6)
            info["copy_time_of_day"] = {"first": {str(k): int(v) for k, v in ist.iloc[[a for _, a, _ in pairs]].dt.strftime("%H:%M:%S").value_counts().head(2).items()},
                                        "second": {str(k): int(v) for k, v in ist.iloc[[b for _, _, b in pairs]].dt.strftime("%H:%M:%S").value_counts().head(2).items()}}
            info["samples"] = [{"key": _iso(k), "first": _row(df, num, ts, ist, a), "second": _row(df, num, ts, ist, b)} for k, a, b in pairs[:SAMPLE]]
            info["tracks"] = _tracks(key, num["close"], dup, pairs)
        out["duplicates"] = info

    vol = num["volume"]
    frac = vol.notna() & (vol != np.floor(vol))
    if frac.any():
        total = float(vol.abs().sum())
        err = float((vol[frac] - np.rint(vol[frac])).abs().sum())
        out["fractional_volume"] = {"candles": int(frac.sum()), "share": round(float(frac.mean()), 4), "first": _iso(key[frac].min()), "last": _iso(key[frac].max()),
                                    "samples": [float(x) for x in vol[frac].head(5)], "smallest_volume_seen": float(vol[frac].min()),
                                    "rounding_would_change_total_by": round(err / total, 8) if total else None}
    out["negative_volume"] = int((vol < 0).sum())
    out["zero_volume"] = int((vol == 0).sum())
    return out


def _tracks(key, close, dup, pairs) -> dict:
    """The two overlapping series told apart by LEVEL: inside the response their order is not reliable (the first copy is sometimes the
    higher price, sometimes the lower). Reports where the ratio between them changes, whether either series jumps, and how each joins the
    dates that appear only once. Facts only: it does not say which series is right."""
    keys = pd.DatetimeIndex([k for k, _, _ in pairs])
    ca = np.array([close[a] for _, a, _ in pairs], dtype=float)
    cb = np.array([close[b] for _, _, b in pairs], dtype=float)
    ok = (ca > 0) & (cb > 0)
    if not ok.any():
        return {}
    keys, ca, cb = keys[ok], ca[ok], cb[ok]
    lo, hi = np.minimum(ca, cb), np.maximum(ca, cb)
    out: dict = {"dates": int(len(keys)), "first_copy_is_lower_on": int((ca < cb).sum())}

    ratio = hi / lo
    smooth = pd.Series(ratio).rolling(5, center=True, min_periods=1).median().to_numpy()      # one odd day does not start a segment
    cuts = [0, *[i for i in range(1, len(smooth)) if abs(smooth[i] / smooth[i - 1] - 1) > RATIO_STEP], len(smooth)]
    segments = [{"from": _iso(keys[s]), "to": _iso(keys[e - 1]), "dates": e - s, "high_over_low": round(float(np.median(ratio[s:e])), 3)}
                for s, e in zip(cuts, cuts[1:])]
    out["ratio_segments"] = {"count": len(segments), "listed": segments[:MAX_SEGMENTS]}

    jumps = {}
    for name, track in (("lower", lo), ("higher", hi)):
        move = track[1:] / track[:-1]
        big = np.where((move > JUMP) | (move < 1 / JUMP))[0]
        jumps[name] = {"count": int(len(big)), "first": [{"date": _iso(keys[i + 1]), "move": round(float(move[i]), 3)} for i in big[:3]]}
    out["big_daily_moves"] = jumps

    single = pd.Series(close[~dup].to_numpy(dtype=float), index=pd.DatetimeIndex(key[~dup])).sort_index()
    before, after = single[single.index < keys[0]], single[single.index > keys[-1]]
    edges = {"first_repeated": {"date": _iso(keys[0]), "lower": float(lo[0]), "higher": float(hi[0])},
             "last_repeated": {"date": _iso(keys[-1]), "lower": float(lo[-1]), "higher": float(hi[-1])}}
    if len(before):
        edges["last_single_before"] = {"date": _iso(before.index[-1]), "close": float(before.iloc[-1])}
    if len(after):
        edges["first_single_after"] = {"date": _iso(after.index[0]), "close": float(after.iloc[0])}
    out["edges"] = edges

    inside = single[(single.index > keys[0]) & (single.index < keys[-1])]
    if len(inside):
        prev = keys.searchsorted(inside.index, side="right") - 1
        near_lo = np.abs(np.log(inside.to_numpy() / lo[prev])) < np.abs(np.log(inside.to_numpy() / hi[prev]))
        out["single_candles_inside"] = {"count": int(len(inside)), "nearer_to_lower": int(near_lo.sum()), "nearer_to_higher": int((~near_lo).sum())}
    return out


def _row(df, num, ts, ist, i) -> dict:
    return {"epoch": int(ts[i]), "ist": _iso(ist[i]), "o": float(num["open"][i]), "h": float(num["high"][i]), "l": float(num["low"][i]),
            "c": float(num["close"][i]), "v": float(num["volume"][i])}


Fetch = Callable[[datetime, datetime], dict]


def run_diagnosis(fetch: Fetch, tf, start: datetime, end: datetime) -> dict:
    """Ask for [start, end) once, analyse the raw answer, then (only if something is wrong) a couple of small probe windows to see whether
    the problem belongs to the whole-history request or to the data itself. ``fetch(start, end)`` returns the raw body or raises."""
    report: dict = {"window": [str(start)[:10], str(end)[:10]], "requests": 0}

    def ask(s, e):
        report["requests"] += 1
        try:
            return fetch(s, e), None
        except AuthError as exc:
            return None, ("auth", exc.message)
        except PermanentError as exc:
            return None, ("rejected", f"{exc.error_code or ''} {exc.message}".strip())
        except TransientError as exc:
            return None, ("temporary", exc.message)
        except ProviderError as exc:
            return None, ("error", exc.message)

    body, err = ask(start, end)
    report["raw"] = body
    if err:
        report["error"] = {"kind": err[0], "message": err[1]}
        if err[0] == "rejected":                                             # e.g. DH-907: is there recent data at all?
            probe, perr = ask(max(start, end - timedelta(days=PROBE_DAYS)), end)
            report["recent_probe"] = ({"error": perr[1]} if perr else analyze_body(tf, probe))
        return report
    report["analysis"] = a = analyze_body(tf, body)
    d = a.get("duplicates")
    if d or a.get("fractional_volume"):
        probes = {}
        if d and d.get("first_key"):
            s0 = datetime.fromisoformat(d["first_key"]).replace(hour=0, minute=0, second=0)
            probes["around_the_first_repeated_date"] = (s0, min(end, s0 + timedelta(days=PROBE_DAYS)))
        probes["most_recent_days"] = (max(start, end - timedelta(days=PROBE_DAYS)), end)
        report["probes"] = {}
        for name, (s, e) in probes.items():
            pb, perr = ask(s, e)
            report["probes"][name] = ({"window": [str(s)[:10], str(e)[:10]], "error": perr[1]} if perr
                                      else {"window": [str(s)[:10], str(e)[:10]], **analyze_body(tf, pb)})
    return report


def _track_lines(t: dict) -> list[str]:
    n, k = t["dates"], t["first_copy_is_lower_on"]
    out = [f"the order of the two copies is NOT reliable: the first copy is the lower-priced one on {k} of {n} repeated dates, so \"keep the first\" "
           "or \"keep the last\" would mix the two series" if 0 < k < n else
           f"the first copy is {'always' if k == n else 'never'} the lower-priced one ({n} repeated dates)"]
    seg = t["ratio_segments"]
    if seg["count"] == 1:
        out.append(f"told apart by level, the higher series is a constant {seg['listed'][0]['high_over_low']:g}x the lower one on every repeated date")
    else:
        listed = "; ".join(f"{x['from'][:10]} to {x['to'][:10]} x{x['high_over_low']:g}" for x in seg["listed"])
        out.append(f"told apart by level, the higher/lower price ratio changes {seg['count'] - 1} time(s): {listed}" + (" ..." if seg["count"] > len(seg["listed"]) else ""))
    mv = t["big_daily_moves"]
    shown = "; ".join(f"{name} {m['count']}" + ("".join(f" ({x['date'][:10]} x{x['move']:g})" for x in m["first"])) for name, m in mv.items())
    out.append(f"day-to-day moves beyond +30%/-23% inside one series: {shown}")
    e = t["edges"]
    fr, lr, b, a = e["first_repeated"], e["last_repeated"], e.get("last_single_before"), e.get("first_single_after")
    out.append((f"just before the repeated stretch the single series closed at {b['close']:g} ({b['date'][:10]}); " if b else "the repeated stretch begins with the first candle; ")
               + f"on {fr['date'][:10]} the two series close at {fr['lower']:g} (lower) and {fr['higher']:g} (higher)")
    out.append(f"on {lr['date'][:10]} (last repeated date) they close at {lr['lower']:g} and {lr['higher']:g}" +
               (f"; the next single candle ({a['date'][:10]}) closes at {a['close']:g}" if a else "; no single candle follows"))
    si = t.get("single_candles_inside")
    if si:
        out.append(f"{si['count']} date(s) inside the stretch appear only once: {si['nearer_to_lower']} are nearer the lower series, {si['nearer_to_higher']} the higher")
    return out


def verdict(report: dict) -> list[str]:
    """Plain-language reading of a report (what it shows; never a guess presented as fact)."""
    out: list[str] = []
    if "error" in report:
        e = report["error"]
        rp = report.get("recent_probe")
        out.append(f"the provider answered the whole-history request with: {e['message']}")
        if rp is not None:
            if "error" in rp:
                out.append(f"the last {PROBE_DAYS} days gave the same kind of answer ({rp['error']}): there may be no data for this instrument at all")
            elif rp.get("rows"):
                out.append(f"BUT the last {PROBE_DAYS} days returned {rp['rows']} candle(s): the instrument HAS data, so the whole-history request is what fails")
            else:
                out.append(f"the last {PROBE_DAYS} days returned no candles: no recent data for this instrument")
        return out
    a = report.get("analysis", {})
    if a.get("problem"):
        return [a["problem"]]
    if not a.get("rows"):
        return ["the response is empty (no candles)"]
    d = a.get("duplicates")
    if d:
        out.append(f"{d['keys_repeated']} date(s) appear more than once ({d['identical']} with identical candles, {d['conflicting']} with DIFFERENT candles), "
                   f"from {d['first_key']} to {d['last_key']}")
        if d["conflicting"]:
            out.append("the two copies sit " + ("next to each other in the response" if d["adjacent_copies_share"] > 0.9 else
                       f"far apart in the response (median {d['median_row_gap_between_copies']:.0f} rows): two blocks, not interleaved"))
            t = d.get("tracks")
            r = d.get("close_ratio_second_over_first")
            if r and not (t and 0 < t["first_copy_is_lower_on"] < t["dates"]):          # when the order flips, a first/second ratio means nothing
                out.append(f"second/first close ratio: median {r['median']}, range {r['min']} to {r['max']}; "
                           + ("a CONSTANT factor (e.g. one adjusted and one unadjusted series)" if r["share_within_1pct_of_median"] > 0.9 and abs(r["median"] - 1) > 0.02
                              else "NOT a constant factor: these look like two different price histories"))
            if t:
                out += _track_lines(t)
        probes = report.get("probes", {})
        around, recent = probes.get("around_the_first_repeated_date"), probes.get("most_recent_days")
        for label, p in (("a small window around the first repeated date", around), ("the most recent days", recent)):
            if p is None:
                continue
            if "error" in p:
                out.append(f"{label}: {p['error']}")
            else:
                dd = p.get("duplicates")
                out.append(f"{label} ({p['window'][0]} to {p['window'][1]}): {p['rows']} candle(s), " +
                           (f"still {dd['keys_repeated']} repeated date(s)" if dd else "no repeated dates"))
    f = a.get("fractional_volume")
    if f:
        out.append(f"{f['candles']} candle(s) ({f['share']:.1%}) have a fractional volume, from {f['first']} to {f['last']} (e.g. {f['samples'][:3]}); "
                   f"rounding them would change the total volume by {f['rounding_would_change_total_by']:.4%}")
    if not out:
        out.append("nothing unusual: the response is clean (no repeated dates, whole-number volumes)")
    return out


def render(symbol: str, security_id: int, report: dict) -> str:
    lines = [f"== {symbol} ({security_id}) · window {report['window'][0]} to {report['window'][1]} · {report['requests']} request(s) =="]
    a = report.get("analysis")
    if a and a.get("rows"):
        lines.append(f"rows {a['rows']:,} · distinct dates/times {a['unique_keys']:,} · {a['first']} to {a['last']} · times of day {a['time_of_day']}")
    d = (a or {}).get("duplicates")
    if d:
        lines.append(f"repeated: {json.dumps({k: v for k, v in d.items() if k not in ('samples', 'tracks')}, default=str)}")
        for s in d.get("samples", []):
            lines.append(f"  {s['key']}: first {s['first']}")
            lines.append(f"  {' ' * len(str(s['key']))}  second {s['second']}")
    lines += ["-> " + v for v in verdict(report)]
    probe = report.get("recent_probe")
    if report.get("error", {}).get("kind") == "rejected" and probe is not None and ("error" in probe or not probe.get("rows")):
        lines.append(f"   if you agree this instrument has no daily data, record it as empty (reviewed ids only, never on a broad run): "
                     f"python -m stockdata download daily --security-ids {security_id} --retry-failed --empty-error-codes DH-907   (docs/RUNBOOK.md, section 6)")
    return "\n".join(lines)
