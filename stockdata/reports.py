"""Integrity checks and per-instrument coverage reports (read-only)."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from stockdata.db import CandleDB


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def integrity_checks(db: CandleDB) -> list[Check]:
    tf, c = db.tf, db.con
    t, k = tf.table, tf.key_col
    one = lambda sql: c.execute(sql).fetchone()[0]
    out: list[Check] = []

    def check(name: str, bad: int, detail: str = "") -> None:
        out.append(Check(name, bad == 0, f"{bad} violation(s)" + (f" {detail}" if detail else "")))

    check("unique key (exchange_segment, security_id, " + k + ")",
          one(f"SELECT count(*) FROM (SELECT 1 FROM {t} GROUP BY exchange_segment, security_id, {k} "
              "HAVING count(*) > 1)"))
    check("prices positive and finite", one(f"SELECT count(*) FROM {t} WHERE open <= 0 OR high <= 0 "
                                            "OR low <= 0 OR close <= 0 OR NOT isfinite(open + high + low + close)"))
    check("OHLC relations (low <= open,close <= high)",
          one(f"SELECT count(*) FROM {t} WHERE low > least(open, close) OR high < greatest(open, close)"),
          "(also listed in data_anomalies; stored as received)")
    check("every candle belongs to a complete window",
          one(f"SELECT count(*) FROM {t} c LEFT JOIN download_windows w USING (window_id) "
              "WHERE w.status IS DISTINCT FROM 'complete'"))
    key_ts = f"CAST(c.{k} AS TIMESTAMP)"
    check("every candle lies inside its window [from, to)",
          one(f"SELECT count(*) FROM {t} c JOIN download_windows w USING (window_id) "
              f"WHERE {key_ts} < w.window_from OR {key_ts} >= w.window_to"))
    check("complete windows have rows, empty windows have none",
          one("SELECT count(*) FROM download_windows WHERE (status = 'complete' AND coalesce(rows_received, 0) = 0) "
              "OR (status = 'empty' AND coalesce(rows_received, 0) <> 0)"))
    check("candles per window match recorded rows",
          one(f"SELECT count(*) FROM download_windows w WHERE w.status = 'complete' AND "
              f"(SELECT count(*) FROM {t} c WHERE c.window_id = w.window_id) > w.rows_received"))
    # A candle that is odd-but-legitimate (a special session) is acceptable only if it was flagged on ingest.
    def unflagged(condition: str, kinds: str) -> int:
        return one(f"SELECT count(*) FROM {t} WHERE ({condition}) AND NOT EXISTS (SELECT 1 FROM data_anomalies a "
                   f"WHERE a.exchange_segment = {t}.exchange_segment AND a.security_id = {t}.security_id "
                   f"AND a.candle_key = CAST({t}.{k} AS TIMESTAMP) AND a.kind IN ({kinds}))")

    n_neg = one(f"SELECT count(*) FROM {t} WHERE volume < 0")
    check("volume non-negative, or flagged as negative_volume", unflagged("volume < 0", "'negative_volume'"),
          f"({n_neg} flagged negative-volume candle(s) present, stored as received)")
    if tf.is_intraday:
        off = (f"hour({k}) * 60 + minute({k}) < 555 OR hour({k}) * 60 + minute({k}) >= 930 "
               f"OR (hour({k}) * 60 + minute({k}) - 555) % 15 <> 0 OR second({k}) <> 0")
        n_special = one(f"SELECT count(*) FROM {t} WHERE {off}")
        check("bars on the 15-minute grid inside 09:15-15:30 IST, or flagged as special sessions",
              unflagged(off, "'outside_session', 'off_grid', 'post_close_bar'"), f"({n_special} flagged special-session bar(s) present)")
        check("epoch_raw round-trips to candle_timestamp (Asia/Kolkata)",
              one(f"SELECT count(*) FROM {t} WHERE epoch_raw <> epoch({k} - INTERVAL 330 MINUTE)"))
    else:
        weekend = f"dayofweek({k}) IN (0, 6)"
        n_weekend = one(f"SELECT count(*) FROM {t} WHERE {weekend}")
        check("candle_date is a weekday, or flagged as a special weekend session",
              unflagged(weekend, "'weekend_candle'"), f"({n_weekend} flagged weekend candle(s) present)")
        check("epoch_raw round-trips to candle_date (midnight Asia/Kolkata)",
              one(f"SELECT count(*) FROM {t} WHERE epoch_raw <> epoch(CAST({k} AS TIMESTAMP) - INTERVAL 330 MINUTE)"))
    return out


def coverage(db: CandleDB) -> pd.DataFrame:
    """One row per instrument: what is stored and what the checkpoints say."""
    tf, c = db.tf, db.con
    t, k = tf.table, tf.key_col
    df = c.execute(f"""
        WITH s AS (SELECT exchange_segment, security_id, count(*) AS candles, min({k}) AS first_candle,
                          max({k}) AS last_candle, count(*) FILTER (WHERE volume = 0) AS zero_volume
                   FROM {t} GROUP BY 1, 2),
             w AS (SELECT exchange_segment, security_id,
                          count(*) FILTER (WHERE status = 'complete') AS win_complete,
                          count(*) FILTER (WHERE status = 'empty') AS win_empty,
                          min(window_from) FILTER (WHERE status IN ('complete','empty')) AS covered_from,
                          max(window_to) FILTER (WHERE status IN ('complete','empty')) AS covered_to
                   FROM download_windows GROUP BY 1, 2),
             a AS (SELECT exchange_segment, security_id, count(*) AS anomalies FROM data_anomalies GROUP BY 1, 2),
             r AS (SELECT exchange_segment, security_id, count(*) AS revisions FROM candle_revisions GROUP BY 1, 2)
        SELECT i.symbol, i.security_id, i.is_active, s.candles, s.first_candle, s.last_candle,
               s.zero_volume, w.win_complete, w.win_empty, w.covered_from, w.covered_to,
               coalesce(a.anomalies, 0) AS anomalies, coalesce(r.revisions, 0) AS revisions
        FROM instruments i
        LEFT JOIN s USING (exchange_segment, security_id) LEFT JOIN w USING (exchange_segment, security_id)
        LEFT JOIN a USING (exchange_segment, security_id) LEFT JOIN r USING (exchange_segment, security_id)
        ORDER BY i.symbol""").df()
    out = pd.DataFrame(db.outstanding_failures(), columns=[
        "exchange_segment", "security_id", "window_from", "window_to", "status", "attempts",
        "error_code", "error_message"])
    for status in ("failed", "retriable"):
        counts = out[out["status"] == status].groupby("security_id").size()
        df[f"outstanding_{status}"] = df["security_id"].map(counts).fillna(0).astype(int)
    if tf.is_intraday:
        per_day = c.execute(f"""SELECT security_id, count(*) AS sessions, median(n) AS median_bars_per_session,
                                       min(n) AS min_bars FROM (SELECT security_id, CAST({k} AS DATE) AS d, count(*) AS n
                                       FROM {t} GROUP BY 1, 2) GROUP BY 1""").df().set_index("security_id")
        df = df.join(per_day, on="security_id")
    else:
        missing = c.execute(f"""
            WITH span AS (SELECT security_id, min({k}) AS lo, max({k}) AS hi FROM {t} GROUP BY 1),
                 present AS (SELECT {k} AS d, count(*) AS p FROM {t} GROUP BY 1),
                 cal AS (SELECT d FROM present WHERE p >= 0.5 * (SELECT count(*) FROM span s WHERE present.d BETWEEN s.lo AND s.hi))
            SELECT s.security_id, count(*) AS missing_market_days FROM span s JOIN cal ON cal.d BETWEEN s.lo AND s.hi
            WHERE NOT EXISTS (SELECT 1 FROM {t} c WHERE c.security_id = s.security_id AND c.{k} = cal.d)
            GROUP BY 1""").df().set_index("security_id")
        df = df.join(missing, on="security_id")
        df["missing_market_days"] = df["missing_market_days"].fillna(0).astype(int)
    return df


def format_report(db: CandleDB) -> str:
    cov = coverage(db)
    checks = integrity_checks(db)
    lines = [f"== {db.tf.name} database: {db.path} ==", "", cov.to_string(index=False, max_colwidth=24), "",
             "integrity checks:"]
    lines += [f"  [{'PASS' if ch.ok else 'FAIL'}] {ch.name}: {ch.detail}" for ch in checks]
    fails = db.outstanding_failures()
    if fails:
        lines += ["", f"{len(fails)} outstanding failed/retriable window(s):"]
        lines += [f"  {f[1]} [{f[2]:%Y-%m-%d} -> {f[3]:%Y-%m-%d}) {f[4]} x{f[5]} {f[6] or ''} {f[7] or ''}"[:160]
                  for f in fails[:50]]
    return "\n".join(lines)


def status_text(db: CandleDB) -> str:
    c = db.con
    rows = lambda sql: c.execute(sql).fetchall()
    out = [f"{db.tf.name} database {db.path}",
           f"  instruments: {rows('SELECT count(*), count(*) FILTER (WHERE is_active) FROM instruments')[0]} (total, active)",
           f"  candles:     {rows(f'SELECT count(*) FROM {db.tf.table}')[0][0]:,}",
           f"  windows:     {dict(rows('SELECT status, count(*) FROM download_windows GROUP BY 1'))}",
           f"  outstanding failed/retriable: {len(db.outstanding_failures())}"]
    for r in rows("SELECT run_id, started_at_utc, status, windows_attempted, rows_inserted, note "
                  "FROM download_runs ORDER BY run_id DESC LIMIT 3"):
        out.append(f"  run {r[0]} {r[1]:%Y-%m-%d %H:%M}Z {r[2]} windows={r[3]} rows+={r[4]} {r[5] or ''}")
    return "\n".join(out)
