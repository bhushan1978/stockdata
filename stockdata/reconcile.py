"""Coverage and reconciliation reports over one candle database: what is there, what is missing, what disagrees.

Everything here is READ-ONLY and nothing is ever filled in: a missing candle stays missing until it is downloaded from a
provider (a targeted repair job does that). The market calendar is not assumed: a date counts as a market SESSION when at
least half of the instruments whose stored history spans it have a candle on it, so a holiday is recognised from the data
instead of from a list that can be wrong. Only an instrument that traded nothing on a real session shows up as 'missing', which
is why the report speaks of candles absent, not of errors.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from stockdata.db import CandleDB
from stockdata.timeutil import ist_now

TOP = 200
SAMPLE_DATES = 5
VOLUME_FACTOR = 20.0
FINAL_BAR_NOT_BEFORE = "15:00:00"       # the last bar starts 15:15 normally and 15:00 for F&O stocks after 3 August 2026: both are final


def _day(db: CandleDB) -> str:
    tf = db.tf
    return f"CAST({tf.key_col} AS DATE)" if tf.is_intraday else tf.key_col


def _frames(db: CandleDB) -> str:
    """CTEs: ``have`` (one row per instrument and day with a candle), ``span`` (its first and last day) and ``cal`` (market sessions)."""
    t, d = db.tf.table, _day(db)
    return f"""have AS (SELECT exchange_segment, security_id, {d} AS d FROM {t} GROUP BY 1, 2, 3),
        span AS (SELECT exchange_segment, security_id, min(d) AS lo, max(d) AS hi FROM have GROUP BY 1, 2),
        present AS (SELECT d, count(*) AS p FROM have GROUP BY 1),
        cal AS (SELECT d FROM present WHERE p >= 0.5 * (SELECT count(*) FROM span s WHERE present.d BETWEEN s.lo AND s.hi))"""


def sessions(db: CandleDB) -> pd.Series:
    return db.con.execute(f"WITH {_frames(db)} SELECT d FROM cal ORDER BY d").df()["d"]


def missing_sessions(db: CandleDB, *, limit: int = TOP) -> dict:
    """Per instrument: market sessions inside its own stored range on which it has no candle."""
    t, k = db.tf.table, db.tf.key_col
    rows = db.con.execute(f"""
        WITH {_frames(db)},
        miss AS (SELECT s.exchange_segment, s.security_id, cal.d FROM span s JOIN cal ON cal.d BETWEEN s.lo AND s.hi
                 ANTI JOIN have h ON h.exchange_segment = s.exchange_segment AND h.security_id = s.security_id AND h.d = cal.d),
        exp AS (SELECT s.security_id, count(*) AS expected FROM span s JOIN cal ON cal.d BETWEEN s.lo AND s.hi GROUP BY 1),
        agg AS (SELECT security_id, count(*) AS missing, min(d) AS first_missing, max(d) AS last_missing,
                       list(d ORDER BY d)[:{SAMPLE_DATES}] AS sample FROM miss GROUP BY 1)
        SELECT i.symbol, a.security_id, e.expected, a.missing, a.missing * 1.0 / e.expected AS missing_ratio,
               a.first_missing, a.last_missing, a.sample, i.is_active
        FROM agg a JOIN exp e USING (security_id) JOIN instruments i ON i.security_id = a.security_id
        ORDER BY a.missing DESC LIMIT {int(limit)}""").fetchall()
    total_inst = db.con.execute(f"WITH {_frames(db)} SELECT count(*) FROM span").fetchone()[0]
    n_sessions, first, last = db.con.execute(f"WITH {_frames(db)} SELECT count(*), min(d), max(d) FROM cal").fetchone()
    affected = db.con.execute(f"""WITH {_frames(db)}, miss AS (SELECT s.security_id FROM span s JOIN cal ON cal.d BETWEEN s.lo AND s.hi
        ANTI JOIN have h ON h.exchange_segment = s.exchange_segment AND h.security_id = s.security_id AND h.d = cal.d)
        SELECT count(DISTINCT security_id), count(*) FROM miss""").fetchone()
    return {"sessions_known": n_sessions, "first_session": _iso(first), "last_session": _iso(last), "instruments_with_data": total_inst,
            "instruments_with_missing": affected[0], "missing_total": affected[1],
            "rows": [{"symbol": r[0], "security_id": r[1], "expected_sessions": r[2], "missing_sessions": r[3], "missing_ratio": round(r[4], 4),
                      "first_missing": _iso(r[5]), "last_missing": _iso(r[6]), "sample": [_iso(x) for x in (r[7] or [])], "is_active": bool(r[8])}
                     for r in rows],
            "note": ("A session is a date on which at least half of the instruments whose history spans it have a candle. A candle that is absent "
                     "is NEVER filled in: an instrument that did not trade on a session simply has none, and a real hole is repaired by "
                     "downloading it again (Fill gaps), not by synthesising it.")}


def final_candle_completeness(db: CandleDB, *, limit: int = TOP) -> dict | None:
    """Intraday only: sessions whose last bar starts before 15:00, i.e. the session looks cut short, and sessions with very few bars."""
    if not db.tf.is_intraday:
        return None
    t, k = db.tf.table, db.tf.key_col
    rows = db.con.execute(f"""
        WITH sess AS (SELECT security_id, CAST({k} AS DATE) AS d, count(*) AS n, max(CAST({k} AS TIME)) AS last_bar FROM {t} GROUP BY 1, 2)
        SELECT i.symbol, s.security_id, count(*) FILTER (WHERE s.last_bar < TIME '{FINAL_BAR_NOT_BEFORE}') AS early_end,
               count(*) FILTER (WHERE s.n < 20) AS thin, count(*) AS sessions, max(s.d) FILTER (WHERE s.last_bar < TIME '{FINAL_BAR_NOT_BEFORE}') AS latest_early
        FROM sess s JOIN instruments i ON i.security_id = s.security_id GROUP BY 1, 2 HAVING early_end > 0
        ORDER BY early_end DESC LIMIT {int(limit)}""").fetchall()
    totals = db.con.execute(f"""WITH sess AS (SELECT security_id, CAST({k} AS DATE) AS d, count(*) AS n, max(CAST({k} AS TIME)) AS last_bar FROM {t} GROUP BY 1, 2)
        SELECT count(*) FILTER (WHERE last_bar < TIME '{FINAL_BAR_NOT_BEFORE}'), count(*) FROM sess""").fetchone()
    return {"sessions_checked": totals[1], "sessions_ending_early": totals[0],
            "rows": [{"symbol": r[0], "security_id": r[1], "sessions_ending_early": r[2], "sessions_with_few_bars": r[3], "sessions": r[4],
                      "latest": _iso(r[5])} for r in rows],
            "note": ("The last bar of a session starts at 15:15, or at 15:00 for F&O stocks after 3 August 2026: both count as complete. "
                     "A session whose last bar starts earlier looks cut short (an illiquid stock's final bars may simply not have traded; "
                     "a special or shortened session is normal).")}


def stale(db: CandleDB, *, limit: int = TOP) -> dict:
    """Active instruments whose last candle is behind the latest market session."""
    d = _day(db)
    t = db.tf.table
    latest = db.con.execute(f"WITH {_frames(db)} SELECT max(d) FROM cal").fetchone()[0]
    last_key = db.con.execute(f"SELECT max({db.tf.key_col}) FROM {t}").fetchone()[0]
    if latest is None:
        return {"latest_session": None, "latest_candle": None, "instruments_current": 0, "instruments_behind": 0, "rows": []}
    rows = db.con.execute(f"""
        WITH {_frames(db)}, last AS (SELECT exchange_segment, security_id, max(d) AS last_day FROM have GROUP BY 1, 2)
        SELECT i.symbol, l.security_id, l.last_day, (SELECT count(*) FROM cal WHERE cal.d > l.last_day) AS sessions_behind, i.is_active
        FROM last l JOIN instruments i ON i.security_id = l.security_id AND i.exchange_segment = l.exchange_segment
        WHERE i.is_active AND l.last_day < (SELECT max(d) FROM cal) ORDER BY sessions_behind DESC, i.symbol LIMIT {int(limit)}""").fetchall()
    behind = db.con.execute(f"""WITH {_frames(db)}, last AS (SELECT exchange_segment, security_id, max(d) AS last_day FROM have GROUP BY 1, 2)
        SELECT count(*) FILTER (WHERE l.last_day < (SELECT max(d) FROM cal)), count(*) FILTER (WHERE l.last_day >= (SELECT max(d) FROM cal))
        FROM last l JOIN instruments i ON i.security_id = l.security_id AND i.exchange_segment = l.exchange_segment WHERE i.is_active""").fetchone()
    today = ist_now().date()
    return {"latest_session": _iso(latest), "latest_candle": str(last_key) if last_key is not None else None,
            "days_since_latest_session": (today - latest).days, "instruments_behind": behind[0], "instruments_current": behind[1],
            "rows": [{"symbol": r[0], "security_id": r[1], "last_candle_day": _iso(r[2]), "sessions_behind": r[3]} for r in rows]}


def abnormal_volume(db: CandleDB, *, since_days: int = 1100, limit: int = 100) -> dict:
    """Sessions whose volume is more than VOLUME_FACTOR times the median of the previous 20 sessions (a lead to look at, not an error)."""
    t, k = db.tf.table, db.tf.key_col
    since = ist_now().date() - timedelta(days=since_days)
    per_day = (f"SELECT security_id, {_day(db)} AS d, sum(volume) AS v FROM {t} WHERE {_day(db)} >= DATE '{since}' GROUP BY 1, 2")
    rows = db.con.execute(f"""
        WITH s AS ({per_day}),
        w AS (SELECT security_id, d, v, median(v) OVER (PARTITION BY security_id ORDER BY d ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS med FROM s)
        SELECT i.symbol, w.security_id, w.d, w.v, w.med, w.v / w.med AS ratio FROM w JOIN instruments i ON i.security_id = w.security_id
        WHERE w.med > 0 AND w.v > {VOLUME_FACTOR} * w.med ORDER BY ratio DESC LIMIT {int(limit)}""").fetchall()
    n = db.con.execute(f"""WITH s AS ({per_day}),
        w AS (SELECT security_id, d, v, median(v) OVER (PARTITION BY security_id ORDER BY d ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS med FROM s)
        SELECT count(*) FROM w WHERE med > 0 AND v > {VOLUME_FACTOR} * med""").fetchone()[0]
    return {"threshold": f"more than {VOLUME_FACTOR:g}x the median of the previous 20 sessions", "since": since.isoformat(), "total": n,
            "rows": [{"symbol": r[0], "security_id": r[1], "date": _iso(r[2]), "volume": int(r[3]), "median_20": float(r[4]), "ratio": round(float(r[5]), 1)} for r in rows],
            "note": "Corporate actions, index inclusions and results days all cause real volume spikes; these are leads, not defects."}


def provider_disagreements(db: CandleDB, *, limit: int = 100) -> dict:
    """Where two providers (or two fetches of the same one) gave different values for the same candle. The stored value was kept."""
    con = db.con
    pairs = con.execute("""SELECT stored_provider, other_provider, count(*), count(DISTINCT security_id), CAST(min(candle_key) AS VARCHAR),
                           CAST(max(candle_key) AS VARCHAR) FROM provider_disagreements GROUP BY 1, 2 ORDER BY 3 DESC""").fetchall()
    same = con.execute("SELECT count(*) FROM candle_revisions WHERE coalesce(old_provider, 'dhan') = coalesce(new_provider, 'dhan')").fetchone()[0]
    sample = con.execute(f"""SELECT i.symbol, d.security_id, CAST(d.candle_key AS VARCHAR), d.stored_provider, d.other_provider, d.old_close, d.new_close,
                             d.old_volume, d.new_volume FROM provider_disagreements d JOIN instruments i ON i.security_id = d.security_id
                             ORDER BY d.detected_at_utc DESC LIMIT {int(limit)}""").fetchall()
    by_instrument = con.execute(f"""SELECT i.symbol, d.security_id, count(*) AS n FROM provider_disagreements d JOIN instruments i USING (security_id)
                                    GROUP BY 1, 2 ORDER BY n DESC LIMIT 50""").fetchall()
    return {"pairs": [{"stored_provider": r[0], "other_provider": r[1], "candles": r[2], "instruments": r[3], "first": r[4], "last": r[5]} for r in pairs],
            "same_provider_revisions": same, "total": sum(r[2] for r in pairs),
            "by_instrument": [{"symbol": r[0], "security_id": r[1], "candles": r[2]} for r in by_instrument],
            "sample": [{"symbol": r[0], "security_id": r[1], "candle": r[2], "stored_provider": r[3], "other_provider": r[4], "stored_close": r[5],
                        "other_close": r[6], "stored_volume": r[7], "other_volume": r[8]} for r in sample],
            "note": ("The candle already stored is kept; the other value is recorded here. Providers can legitimately differ (their own "
                     "corporate-action adjustment, a late revision). Replacing a stored candle is a separate, explicit, audited repair.")}


def lifecycle(db: CandleDB) -> dict:
    """Identity over time, from the instrument master loads: renames, symbols reused by different securities, shared ISINs,
    new listings and instruments that left the master. Candles are keyed by security id, never by symbol, so a renamed or reused
    symbol can never join two different securities."""
    con = db.con
    today = ist_now().date()
    renamed = con.execute("""SELECT security_id, list(symbol ORDER BY valid_from) AS names, min(valid_from), max(valid_from) FROM instrument_history
                             GROUP BY 1 HAVING count(DISTINCT symbol) > 1 ORDER BY max(valid_from) DESC LIMIT 200""").fetchall()
    reused = con.execute("""SELECT symbol, list(DISTINCT security_id), count(DISTINCT security_id) FROM instrument_history
                            GROUP BY 1 HAVING count(DISTINCT security_id) > 1 ORDER BY 1 LIMIT 200""").fetchall()
    isin = con.execute("""SELECT isin, list(DISTINCT security_id), list(DISTINCT symbol) FROM instrument_history
                          WHERE isin IS NOT NULL AND isin <> '' GROUP BY 1 HAVING count(DISTINCT security_id) > 1 ORDER BY 1 LIMIT 200""").fetchall()
    new = con.execute("SELECT symbol, security_id, first_seen FROM instruments WHERE is_active AND first_seen >= ? ORDER BY first_seen DESC LIMIT 200",
                      [today - timedelta(days=30)]).fetchall()
    first_load = con.execute("SELECT min(loaded_at_utc) FROM instrument_master_loads").fetchone()[0]
    left = con.execute(f"""SELECT i.symbol, i.security_id, i.last_seen, (SELECT count(*) FROM {db.tf.table} c WHERE c.security_id = i.security_id),
                           (SELECT max({db.tf.key_col}) FROM {db.tf.table} c WHERE c.security_id = i.security_id)
                           FROM instruments i WHERE NOT i.is_active ORDER BY i.last_seen DESC LIMIT 200""").fetchall()
    return {"renamed": [{"security_id": r[0], "symbols": r[1], "from": _iso(r[2]), "latest_change": _iso(r[3])} for r in renamed],
            "symbol_reused": [{"symbol": r[0], "security_ids": r[1]} for r in reused],
            "isin_shared": [{"isin": r[0], "security_ids": r[1], "symbols": r[2]} for r in isin],
            "new_listings": [{"symbol": r[0], "security_id": r[1], "first_seen": _iso(r[2])} for r in new],
            "left_the_master": [{"symbol": r[0], "security_id": r[1], "last_seen": _iso(r[2]), "candles": r[3], "last_candle": str(r[4]) if r[4] is not None else None}
                                for r in left],
            "master_history_since": str(first_load) if first_load else None,
            "notes": ["Candles belong to a security id. Two securities that share a display symbol over time are two separate series and are never merged.",
                      "A rename is recorded when the master's symbol for the SAME security id changes. A company that changes its security id "
                      "(a merger, a new listing) is NOT linked automatically: that needs an authoritative source and a person's decision.",
                      "The Dhan instrument master carries no suspension flag; 'left the master' is the closest signal for a suspension or delisting and "
                      "needs confirming against the exchange.",
                      "History starts at the first instrument-master load, so earlier renames cannot be known."]}


def build(db: CandleDB) -> dict:
    out = {"missing_sessions": missing_sessions(db), "stale": stale(db), "abnormal_volume": abnormal_volume(db),
           "provider_disagreements": provider_disagreements(db)}
    fin = final_candle_completeness(db)
    if fin is not None:
        out["final_candle"] = fin
    return out


def _iso(x) -> str | None:
    if x is None:
        return None
    return x.isoformat() if isinstance(x, date) else str(x)
