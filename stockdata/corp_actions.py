"""Corporate-action reference, verification against candles, and adjusted views.

Principles
* Raw candles (``candles_*``) are never modified. Adjusted data lives only in
  the ``*_adjusted`` views, which read ``adjustment_segments``.
* Nothing is assumed about whether Dhan's history is raw or adjusted. For each
  event the candles around the ex-date are compared with the expected factor
  (and, when supplied, with an independent raw NSE close). The outcome is
  stored per database in ``corporate_action_checks``.
* An event is applied only when it is source-confirmed AND this database's
  candles show the raw (unadjusted) jump. If the data already looks adjusted
  the event is marked ``source_adjusted`` and is never applied again.
* Only SPLIT and BONUS factors are computed automatically (from published
  ratios). RIGHTS, DEMERGER, MERGER and everything else stay ``unresolved``
  unless a human supplies a factor and verifies it. DIVIDENDs are recorded but
  are never part of the price-adjusted series (no total-return adjustment).
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from stockdata.timeutil import utcnow

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS corporate_actions (
        action_id VARCHAR PRIMARY KEY,
        exchange_segment VARCHAR NOT NULL, security_id BIGINT, symbol VARCHAR NOT NULL, isin VARCHAR,
        event_type VARCHAR NOT NULL,
        announcement_date DATE, ex_date DATE NOT NULL, record_date DATE,
        ratio_text VARCHAR,
        old_face_value DOUBLE, new_face_value DOUBLE,
        bonus_new DOUBLE, bonus_held DOUBLE,
        rights_new DOUBLE, rights_held DOUBLE, issue_price DOUBLE,
        cash_amount DOUBLE, related_security_id BIGINT,
        price_factor DOUBLE, volume_factor DOUBLE,
        adjusts_price BOOLEAN NOT NULL, factor_origin VARCHAR,
        source VARCHAR NOT NULL, source_ref VARCHAR, source_confirmed BOOLEAN NOT NULL,
        verification_status VARCHAR NOT NULL, verification_note VARCHAR,
        apply_adjustment BOOLEAN NOT NULL DEFAULT false,
        imported_at_utc TIMESTAMP NOT NULL, processed_at_utc TIMESTAMP,
        source_url VARCHAR, evidence VARCHAR, retrieved_at_utc TIMESTAMP, version INTEGER DEFAULT 1,
        affected_from DATE, affected_to DATE, application_status VARCHAR, applied_version INTEGER)""",
    """CREATE TABLE IF NOT EXISTS corporate_action_checks (
        action_id VARCHAR PRIMARY KEY, checked_at_utc TIMESTAMP NOT NULL,
        prev_session DATE, ex_session DATE,
        prev_close DOUBLE, ex_open DOUBLE, ex_close DOUBLE,
        observed_ratio DOUBLE, expected_factor DOUBLE,
        vol_before DOUBLE, vol_after DOUBLE, volume_ratio DOUBLE,
        ref_close DOUBLE, ref_ratio DOUBLE,
        basis VARCHAR NOT NULL, detail VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS adjustment_segments (
        exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL,
        valid_from DATE NOT NULL, valid_to DATE NOT NULL,
        cum_price_factor DOUBLE NOT NULL, cum_volume_factor DOUBLE NOT NULL,
        n_events INTEGER NOT NULL, built_at_utc TIMESTAMP NOT NULL,
        PRIMARY KEY (exchange_segment, security_id, valid_from))""",
    """CREATE TABLE IF NOT EXISTS corporate_action_revisions (
        revision_id BIGINT PRIMARY KEY, action_id VARCHAR NOT NULL, version INTEGER NOT NULL, changed_at_utc TIMESTAMP NOT NULL,
        actor VARCHAR, change VARCHAR NOT NULL, old_value VARCHAR, new_value VARCHAR, note VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS adjustment_runs (
        version INTEGER PRIMARY KEY, applied_at_utc TIMESTAMP NOT NULL, events_applied INTEGER NOT NULL, segments INTEGER NOT NULL,
        fingerprint VARCHAR NOT NULL, event_ids VARCHAR, validation_failures INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS adjustment_validations (
        version INTEGER NOT NULL, action_id VARCHAR NOT NULL, checked_at_utc TIMESTAMP NOT NULL, ok BOOLEAN NOT NULL,
        prev_session DATE, ex_session DATE, adj_prev_close DOUBLE, ex_open DOUBLE, gap_ratio DOUBLE, adj_volume_ratio DOUBLE,
        detail VARCHAR, PRIMARY KEY (version, action_id))""",
    """CREATE TABLE IF NOT EXISTS adjustment_scan_findings (
        exchange_segment VARCHAR NOT NULL, security_id BIGINT NOT NULL, event_date DATE NOT NULL,
        prev_close DOUBLE, next_open DOUBLE, ratio DOUBLE, volume_ratio DOUBLE,
        nearest_ratio VARCHAR, matched_action_id VARCHAR, found_at_utc TIMESTAMP NOT NULL,
        PRIMARY KEY (exchange_segment, security_id, event_date))""",
]

ADDITIVE_COLUMNS = [("corporate_actions", "source_url", "VARCHAR"), ("corporate_actions", "evidence", "VARCHAR"),
                    ("corporate_actions", "retrieved_at_utc", "TIMESTAMP"), ("corporate_actions", "version", "INTEGER DEFAULT 1"),
                    ("corporate_actions", "affected_from", "DATE"), ("corporate_actions", "affected_to", "DATE"),
                    ("corporate_actions", "application_status", "VARCHAR"), ("corporate_actions", "applied_version", "INTEGER")]

EVENT_TYPES = ("SPLIT", "BONUS", "RIGHTS", "DIVIDEND", "MERGER", "DEMERGER", "SYMBOL_CHANGE", "OTHER")
AUTO_FACTOR = {"SPLIT", "BONUS"}                      # computed from published ratios
PRICE_ADJUSTING = {"SPLIT", "BONUS", "RIGHTS", "DEMERGER"}   # may enter the price-adjusted series
STATUSES = ("proposed", "verified", "source_adjusted", "unresolved", "recorded_not_adjusted", "rejected")
FAR_PAST, FAR_FUTURE = date(1900, 1, 1), date(9999, 12, 31)

MAX_SESSION_GAP_DAYS = 7          # prev/ex sessions further apart than this are not comparable
MIN_SEPARATION = math.log(1.25)   # factors closer to 1 than this cannot be told apart from normal moves
PRICE_TOL = 0.12                  # |ln| distance accepted around the raw or adjusted expectation
REF_TOL = 0.05                    # tolerance when comparing with an independent raw close


# ----------------------------------------------------------------------------- factors
def split_factors(old_fv: float, new_fv: float) -> tuple[float, float]:
    """Face-value split/consolidation: price x new/old, volume x old/new."""
    if not (old_fv > 0 and new_fv > 0):
        raise ValueError("face values must be positive")
    return new_fv / old_fv, old_fv / new_fv


def bonus_factors(new_shares: float, held: float) -> tuple[float, float]:
    """Bonus of ``new_shares`` for every ``held``: shares x (new+held)/held."""
    if not (new_shares > 0 and held > 0):
        raise ValueError("bonus ratio terms must be positive")
    return held / (new_shares + held), (new_shares + held) / held


def rights_terp_factor(cum_close: float, issue_price: float, new: float, held: float) -> float:
    """Theoretical ex-rights price over the cum-rights close. Price only; volume is left unadjusted."""
    if not (cum_close > 0 and issue_price > 0 and new > 0 and held > 0):
        raise ValueError("rights inputs must be positive")
    return ((held * cum_close + new * issue_price) / (held + new)) / cum_close


# ----------------------------------------------------------------------- NSE purpose text
_NUM = r"([\d]+(?:\.\d+)?)"
_RS = r"r[se]s?\.?\s*"


def parse_nse_purpose(purpose: str, face_value: float | None = None) -> dict:
    """Conservative parser for NSE's free-text corporate-action purpose.

    The text layouts handled here are the commonly published ones and are
    UNVERIFIED against NSE's current export; anything not matched exactly
    comes back as OTHER so it is flagged unresolved instead of guessed.
    """
    text = " ".join(str(purpose).split())
    low = text.lower()
    kinds = [k for k, pat in (("SPLIT", r"split|sub-?\s*division"), ("BONUS", r"\bbonus"),
                              ("RIGHTS", r"\brights?\b"), ("DIVIDEND", r"dividend"),
                              ("DEMERGER", r"demerger|de-merger|spin"), ("MERGER", r"(?<![a-z-])merger|amalgamation"),
                              ("SYMBOL_CHANGE", r"change in name|name change|symbol change"))
             if re.search(pat, low)]
    if len(kinds) != 1:
        return {"event_type": "OTHER", "ratio_text": text,
                "note": "multiple or unrecognised event kinds in one purpose" if kinds else "unrecognised purpose"}
    kind, out = kinds[0], {"event_type": kinds[0], "ratio_text": text}
    if kind == "SPLIT":
        m = re.search(r"from\s+" + _RS + _NUM + r".*?\bto\s+" + _RS + _NUM, low)
        if m:
            out.update(old_face_value=float(m.group(1)), new_face_value=float(m.group(2)))
    elif kind == "BONUS":
        m = re.search(r"(\d+)\s*:\s*(\d+)", low)
        if m:
            out.update(bonus_new=float(m.group(1)), bonus_held=float(m.group(2)))
    elif kind == "RIGHTS":
        m, p = re.search(r"(\d+)\s*:\s*(\d+)", low), re.search(r"premium\s*" + _RS + _NUM, low)
        if m:
            out.update(rights_new=float(m.group(1)), rights_held=float(m.group(2)))
        if p and face_value:
            out["issue_price"] = face_value + float(p.group(1))
    elif kind == "DIVIDEND":
        amt, pct = re.search(_RS + _NUM, low), re.search(_NUM + r"\s*%", low)
        if amt:
            out["cash_amount"] = float(amt.group(1))
        elif pct and face_value:
            out["cash_amount"] = float(pct.group(1)) / 100 * face_value
    return out


# ---------------------------------------------------------------------------------------- revisions
def _next(con, table: str, column: str) -> int:
    return con.execute(f"SELECT coalesce(max({column}), 0) + 1 FROM {table}").fetchone()[0]


def revise(con, action_id_: str, change: str, old, new, *, actor: str = "system", note: str | None = None, bump: bool = True) -> int:
    """Append one revision record and (by default) bump the event's version. Returns the new version.

    Nothing about an event is ever overwritten without a trace: the ledger row carries the CURRENT state and this table
    carries every earlier one (what changed, from what, to what, by whom)."""
    row = con.execute("SELECT coalesce(version, 1) FROM corporate_actions WHERE action_id = ?", [action_id_]).fetchone()
    version = (row[0] if row else 1) + (1 if bump and change != "created" else 0)
    if bump and change != "created":
        con.execute("UPDATE corporate_actions SET version = ? WHERE action_id = ?", [version, action_id_])
    con.execute("INSERT INTO corporate_action_revisions (revision_id, action_id, version, changed_at_utc, actor, change, old_value, new_value, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [_next(con, "corporate_action_revisions", "revision_id"), action_id_, version, utcnow(), actor, change,
                 None if old is None else str(old), None if new is None else str(new), note])
    return version


# ------------------------------------------------------------------------------- import
def action_id(symbol: str, event_type: str, ex_date: date, ratio_text: str, source: str) -> str:
    key = "|".join([symbol.upper(), event_type, ex_date.isoformat(), " ".join(ratio_text.split()).lower(),
                    source.lower()])
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _num(v) -> float | None:
    try:
        return None if v is None or str(v).strip() == "" or pd.isna(v) else float(v)
    except (TypeError, ValueError):
        return None


def _date(v) -> date | None:
    if v is None or str(v).strip() in ("", "-", "nan", "NaT"):
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class ImportResult:
    inserted: int = 0
    existing: int = 0
    skipped: int = 0
    by_status: dict | None = None


def import_actions(con, rows: list[dict], *, source: str, source_ref: str, source_confirmed: bool) -> ImportResult:
    """Insert parsed events (dicts using the reference-CSV column names).

    Existing action_ids are left untouched, so re-importing never resets a
    verification decision."""
    res, now = ImportResult(by_status={}), utcnow()
    for r in rows:
        ex = _date(r.get("ex_date"))
        etype = str(r.get("event_type", "")).upper().strip()
        symbol = str(r.get("symbol", "")).strip().upper()
        if ex is None or not symbol or etype not in EVENT_TYPES:
            res.skipped += 1
            continue
        inst = con.execute("SELECT exchange_segment, security_id FROM instruments WHERE upper(symbol) = ? "
                           "ORDER BY is_active DESC", [symbol]).fetchall()
        ratio_text = str(r.get("ratio_text", "") or "")
        f = {k: _num(r.get(k)) for k in ("old_face_value", "new_face_value", "bonus_new", "bonus_held",
                                         "rights_new", "rights_held", "issue_price", "cash_amount")}
        pf, vf, origin, note = _num(r.get("price_factor_override")), _num(r.get("volume_factor_override")), None, ""
        status = "proposed"
        try:
            if pf is not None:
                origin = "manual_override"
                vf = vf if vf is not None else (1 / pf if etype in AUTO_FACTOR else 1.0)
            elif etype == "SPLIT" and f["old_face_value"] and f["new_face_value"]:
                (pf, vf), origin = split_factors(f["old_face_value"], f["new_face_value"]), "computed_from_ratio"
            elif etype == "BONUS" and f["bonus_new"] and f["bonus_held"]:
                (pf, vf), origin = bonus_factors(f["bonus_new"], f["bonus_held"]), "computed_from_ratio"
        except ValueError as exc:
            note = f"invalid ratio: {exc}"
        if etype in AUTO_FACTOR and pf is None:
            status, note = "unresolved", note or "ratio terms missing or unparseable"
        elif etype in ("DIVIDEND", "SYMBOL_CHANGE"):
            status, note = "recorded_not_adjusted", "not part of the price-adjusted series"
        elif etype == "RIGHTS" and pf is None:
            status, note = "unresolved", "rights factor (TERP) needs the cum-rights close; computed at evaluate, never auto-applied"
        elif etype == "MERGER":
            status, note = "unresolved", "mergers are not price-adjusted; the successor mapping needs manual review"
        elif etype in ("DEMERGER", "OTHER") and pf is None:
            status, note = "unresolved", "scheme-specific methodology; supply a verified factor to adjust"
        if not inst:
            status, note = "unresolved", (note + "; " if note else "") + "symbol not in the instruments table"
        elif len({i[1] for i in inst}) > 1:
            status, note = "unresolved", (note + "; " if note else "") + "symbol maps to several security ids"
        seg, sec = (inst[0][0], inst[0][1]) if inst and len({i[1] for i in inst}) == 1 else \
            ("NSE_EQ", None)
        adjusts = etype in PRICE_ADJUSTING
        aid = action_id(symbol, etype, ex, ratio_text, source)
        if con.execute("SELECT 1 FROM corporate_actions WHERE action_id = ?", [aid]).fetchone():
            res.existing += 1
            continue
        con.execute("""INSERT INTO corporate_actions (action_id, exchange_segment, security_id, symbol, isin, event_type,
                announcement_date, ex_date, record_date, ratio_text, old_face_value, new_face_value, bonus_new, bonus_held,
                rights_new, rights_held, issue_price, cash_amount, related_security_id, price_factor, volume_factor, adjusts_price,
                factor_origin, source, source_ref, source_confirmed, verification_status, verification_note, apply_adjustment,
                imported_at_utc, processed_at_utc, source_url, evidence, retrieved_at_utc, version, application_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'not_applied')""",
                    [aid, seg, sec, symbol, (str(r.get("isin") or "") or None), etype,
                     _date(r.get("announcement_date")), ex, _date(r.get("record_date")), ratio_text,
                     f["old_face_value"], f["new_face_value"], f["bonus_new"], f["bonus_held"],
                     f["rights_new"], f["rights_held"], f["issue_price"], f["cash_amount"], None,
                     pf, vf, adjusts, origin, source, source_ref or str(r.get("source_ref") or ""),
                     bool(source_confirmed), status, note or None, False, now, None,
                     str(r.get("source_url") or "") or None, str(r.get("evidence") or "") or None, now])
        revise(con, aid, "created", None, status, actor="import", note=f"{source}: {source_ref or r.get('source_ref') or ''}".strip(": "), bump=False)
        res.inserted += 1
        res.by_status[status] = res.by_status.get(status, 0) + 1
    return res


NSE_ALIASES = {"symbol": ("SYMBOL",), "purpose": ("PURPOSE", "SUBJECT"), "face_value": ("FACE VALUE", "FACEVALUE", "FV"),
               "ex_date": ("EX-DATE", "EX DATE", "EXDATE"), "record_date": ("RECORD DATE", "RECORDDATE"),
               "isin": ("ISIN",), "series": ("SERIES",)}


def rows_from_nse_csv(df: pd.DataFrame) -> list[dict]:
    cols = {c.strip().upper(): c for c in df.columns}
    pick = {k: next((cols[a] for a in v if a in cols), None) for k, v in NSE_ALIASES.items()}
    missing = [k for k in ("symbol", "purpose", "ex_date") if pick[k] is None]
    if missing:
        raise ValueError(f"NSE corporate-action CSV lacks {missing}; columns found: {list(df.columns)}")
    out = []
    for _, row in df.iterrows():
        fv = _num(row[pick["face_value"]]) if pick["face_value"] else None
        parsed = parse_nse_purpose(row[pick["purpose"]], fv)
        parsed.update(symbol=row[pick["symbol"]], ex_date=row[pick["ex_date"]],
                      record_date=row[pick["record_date"]] if pick["record_date"] else None,
                      isin=row[pick["isin"]] if pick["isin"] else None)
        out.append(parsed)
    return out


# -------------------------------------------------------------------------- evaluation
def sessions_around(con, tf, exchange_segment: str, security_id: int, ex_date: date, days: int = 25) -> pd.DataFrame:
    """Per-session (d, open, close, volume) around ``ex_date``.
    Intraday: first bar open, last bar close, summed volume of each session."""
    lo, hi = ex_date - timedelta(days=days), ex_date + timedelta(days=days)
    if tf.is_intraday:
        sql = (f"SELECT CAST({tf.key_col} AS DATE) AS d, arg_min(open, {tf.key_col}) AS open, "
               f"arg_max(close, {tf.key_col}) AS close, sum(volume) AS volume FROM {tf.table} "
               f"WHERE exchange_segment = ? AND security_id = ? AND CAST({tf.key_col} AS DATE) BETWEEN ? AND ? "
               "GROUP BY d ORDER BY d")
    else:
        sql = (f"SELECT {tf.key_col} AS d, open, close, volume FROM {tf.table} WHERE exchange_segment = ? "
               f"AND security_id = ? AND {tf.key_col} BETWEEN ? AND ? ORDER BY d")
    return con.execute(sql, [exchange_segment, security_id, lo, hi]).df()


def classify_basis(observed_ratio: float | None, expected_factor: float, volume_ratio: float | None,
                   expected_volume_factor: float | None, ref_ratio: float | None = None) -> tuple[str, str]:
    """Is the candle history raw (unadjusted) or already adjusted for this event?

    ``observed_ratio`` = first open on/after the ex-date / last close before it.
    ``ref_ratio`` = Dhan close on the last pre-ex session / an independent RAW close for that session.
    Returns (basis, reason) with basis in unadjusted | adjusted | ambiguous.
    """
    sep = abs(math.log(expected_factor))
    if ref_ratio is not None:                    # independent raw price: decisive
        d_raw, d_adj = abs(math.log(ref_ratio)), abs(math.log(ref_ratio / expected_factor))
        if d_raw <= REF_TOL and d_adj > 2 * REF_TOL:
            return "unadjusted", f"Dhan pre-ex close equals the independent raw close (ratio {ref_ratio:.4f})"
        if d_adj <= REF_TOL and d_raw > 2 * REF_TOL:
            return "adjusted", f"Dhan pre-ex close = raw close x factor (ratio {ref_ratio:.4f})"
        return "ambiguous", f"independent comparison inconclusive (ratio {ref_ratio:.4f}, factor {expected_factor:.4f})"
    if observed_ratio is None or observed_ratio <= 0:
        return "ambiguous", "no comparable prices"
    if sep < MIN_SEPARATION - 1e-9:   # (1:4 bonus = 0.8 sits exactly on the boundary; keep it testable)
        return "ambiguous", f"factor {expected_factor:.4f} too close to 1 to separate from normal price moves"
    d_raw, d_adj = abs(math.log(observed_ratio / expected_factor)), abs(math.log(observed_ratio))
    tol = min(PRICE_TOL, sep / 2.5)
    if d_raw <= tol and d_adj > sep / 2:
        verdict = "unadjusted"
    elif d_adj <= tol and d_raw > sep / 2:
        verdict = "adjusted"
    else:
        return "ambiguous", f"gap ratio {observed_ratio:.4f} matches neither raw ({expected_factor:.4f}) nor adjusted (1.0)"
    reason = f"gap ratio {observed_ratio:.4f} vs factor {expected_factor:.4f}"
    if volume_ratio and expected_volume_factor and expected_volume_factor > 0 and expected_volume_factor != 1:
        score = math.log(volume_ratio) / math.log(expected_volume_factor)   # ~1 if volume is raw, ~0 if adjusted
        if (verdict == "unadjusted" and score < 0.25) or (verdict == "adjusted" and score > 0.75):
            return "ambiguous", f"{reason}; volume ratio {volume_ratio:.2f} contradicts the price evidence"
        reason += f"; volume ratio {volume_ratio:.2f}"
    return verdict, reason


def evaluate_event(sessions: pd.DataFrame, ex_date: date, factor: float, vol_factor: float | None,
                   ref_close: float | None = None) -> dict:
    """Pure function: compare sessions before/after ``ex_date`` with the expected factor."""
    s = sessions.copy()
    if s.empty:
        return {"basis": "no_data", "detail": "no candles near the ex-date"}
    s["d"] = pd.to_datetime(s["d"]).dt.date
    before, after = s[s["d"] < ex_date], s[s["d"] >= ex_date]
    if before.empty or after.empty:
        return {"basis": "no_data", "detail": "no session on both sides of the ex-date"}
    prev, ex = before.iloc[-1], after.iloc[0]
    gap = (ex["d"] - prev["d"]).days
    out = {"prev_session": prev["d"], "ex_session": ex["d"], "prev_close": float(prev["close"]),
           "ex_open": float(ex["open"]), "ex_close": float(ex["close"]), "expected_factor": factor}
    if gap > MAX_SESSION_GAP_DAYS or (ex["d"] - ex_date).days > MAX_SESSION_GAP_DAYS:
        return {**out, "basis": "no_data", "detail": f"sessions {prev['d']} and {ex['d']} are not adjacent enough to compare"}
    obs = out["ex_open"] / out["prev_close"] if out["prev_close"] > 0 else None
    vb, va = before.tail(5)["volume"].median(), after.head(5)["volume"].median()
    vr = float(va / vb) if vb and va and vb > 0 and va > 0 else None
    ref_ratio = out["prev_close"] / ref_close if ref_close else None
    basis, reason = classify_basis(obs, factor, vr, vol_factor, ref_ratio)
    return {**out, "observed_ratio": obs, "vol_before": float(vb) if vb == vb else None,
            "vol_after": float(va) if va == va else None, "volume_ratio": vr, "ref_close": ref_close,
            "ref_ratio": ref_ratio, "basis": basis, "detail": reason}


def providers_around(con, tf, exchange_segment: str, security_id: int, ex_date: date, days: int = 25) -> list[str]:
    """The data providers that supplied this instrument's candles near the ex-date (NULL = recorded before provider tracking = Dhan)."""
    lo, hi = ex_date - timedelta(days=days), ex_date + timedelta(days=days)
    key = f"CAST({tf.key_col} AS DATE)" if tf.is_intraday else tf.key_col
    return sorted(r[0] for r in con.execute(
        f"SELECT DISTINCT coalesce(provider, 'dhan') FROM {tf.table} WHERE exchange_segment = ? AND security_id = ? AND {key} BETWEEN ? AND ?",
        [exchange_segment, security_id, lo, hi]).fetchall())


def evaluate_all(con, tf, *, reference: dict[tuple[str, date], float] | None = None) -> pd.DataFrame:
    """Check every price-adjusting event against this database's candles and update its status."""
    reference = reference or {}
    now = utcnow()
    events = con.execute("""SELECT action_id, exchange_segment, security_id, symbol, event_type, ex_date,
        price_factor, volume_factor, source_confirmed, verification_status, verification_note, issue_price,
        rights_new, rights_held, apply_adjustment FROM corporate_actions WHERE adjusts_price ORDER BY symbol, ex_date""").fetchall()
    rows = []
    for (aid, seg, sec, sym, etype, ex, pf, vf, confirmed, status, note, issue, r_new, r_held, was_applied) in events:
        if note and str(note).startswith("manual:"):
            continue                                                  # a human decision is never overwritten
        if sec is None:
            res = {"basis": "no_data", "detail": "instrument not resolved"}
        else:
            sess = sessions_around(con, tf, seg, sec, ex)
            if etype == "RIGHTS":                                     # TERP needs the raw cum-rights close
                before = sess[pd.to_datetime(sess["d"]).dt.date < ex]
                if pf is None and not before.empty and issue and r_new and r_held:
                    pf = rights_terp_factor(float(before.iloc[-1]["close"]), issue, r_new, r_held)
                    con.execute("UPDATE corporate_actions SET price_factor = ?, volume_factor = 1.0, "
                                "factor_origin = 'terp_computed' WHERE action_id = ?", [pf, aid])
                res = {"basis": "ambiguous", "detail": "rights: factor computed (TERP) but the price effect is too small "
                                                       "to verify from candles; needs manual verification"}
            elif pf is None:
                res = {"basis": "no_data", "detail": "no factor to test"}
            else:
                prev_date = None
                res = evaluate_event(sess, ex, pf, vf, None)
                prev_date = res.get("prev_session")
                ref = reference.get((sym, prev_date)) if prev_date else None
                if ref:
                    res = evaluate_event(sess, ex, pf, vf, ref)
                provs = providers_around(con, tf, seg, sec, ex)
                if len(provs) > 1 and res["basis"] in ("unadjusted", "adjusted"):
                    # providers can differ in how (and whether) their history is adjusted, so a before/after comparison that
                    # spans two sources proves nothing about either: a person must look at it
                    res = {**res, "basis": "ambiguous",
                           "detail": f"candles around the ex-date come from several providers ({', '.join(provs)}); "
                                     "their adjustment bases can differ, so this cannot be verified automatically"}
        basis = res["basis"]
        if basis == "unadjusted" and confirmed:
            new_status, apply_, msg = "verified", bool(pf is not None), "candles show the raw jump; source confirmed"
        elif basis == "unadjusted":
            new_status, apply_, msg = "proposed", False, "candles show the raw jump but the source is not confirmed"
        elif basis == "adjusted":
            new_status, apply_, msg = "source_adjusted", False, "history already adjusted by the source - NOT applied again"
        else:
            new_status, apply_, msg = "unresolved", False, f"cannot verify ({basis})"
        con.execute("""INSERT INTO corporate_action_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (action_id) DO UPDATE SET checked_at_utc = excluded.checked_at_utc,
              prev_session = excluded.prev_session, ex_session = excluded.ex_session,
              prev_close = excluded.prev_close, ex_open = excluded.ex_open, ex_close = excluded.ex_close,
              observed_ratio = excluded.observed_ratio, expected_factor = excluded.expected_factor,
              vol_before = excluded.vol_before, vol_after = excluded.vol_after, volume_ratio = excluded.volume_ratio,
              ref_close = excluded.ref_close, ref_ratio = excluded.ref_ratio, basis = excluded.basis,
              detail = excluded.detail""",
                    [aid, now, res.get("prev_session"), res.get("ex_session"), res.get("prev_close"),
                     res.get("ex_open"), res.get("ex_close"), res.get("observed_ratio"), res.get("expected_factor", pf),
                     res.get("vol_before"), res.get("vol_after"), res.get("volume_ratio"), res.get("ref_close"),
                     res.get("ref_ratio"), basis, res.get("detail")])
        if etype == "RIGHTS":
            new_status, apply_ = "unresolved", False
        con.execute("UPDATE corporate_actions SET verification_status = ?, apply_adjustment = ?, "
                    "verification_note = ?, processed_at_utc = ? WHERE action_id = ?",
                    [new_status, apply_, f"{msg}: {res.get('detail')}", now, aid])
        if new_status != status or bool(apply_) != bool(was_applied):
            revise(con, aid, "verification", f"{status}/{'apply' if was_applied else 'hold'}", f"{new_status}/{'apply' if apply_ else 'hold'}",
                   actor="evaluate", note=res.get("detail"))
        rows.append({"symbol": sym, "type": etype, "ex_date": ex, "factor": pf, "basis": basis,
                     "status": new_status, "applied": apply_, "detail": res.get("detail")})
    return pd.DataFrame(rows)


def set_manual_status(con, action_id_: str, status: str, note: str) -> None:
    """Explicit, audited human decision (e.g. a verified rights/demerger factor)."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if not note.strip():
        raise ValueError("a note stating who verified what, and against which source, is required")
    row = con.execute("SELECT adjusts_price, price_factor FROM corporate_actions WHERE action_id = ?",
                      [action_id_]).fetchone()
    if row is None:
        raise ValueError(f"unknown action_id {action_id_}")
    apply_ = status == "verified" and bool(row[0]) and row[1] is not None
    before = con.execute("SELECT verification_status, apply_adjustment FROM corporate_actions WHERE action_id = ?", [action_id_]).fetchone()
    con.execute("UPDATE corporate_actions SET verification_status = ?, apply_adjustment = ?, "
                "verification_note = ?, processed_at_utc = ? WHERE action_id = ?",
                [status, apply_, f"manual: {note.strip()}", utcnow(), action_id_])
    revise(con, action_id_, "manual_decision", f"{before[0]}/{'apply' if before[1] else 'hold'}", f"{status}/{'apply' if apply_ else 'hold'}",
           actor="person", note=note.strip())


# ------------------------------------------------------------------ segments and views
def build_segments(events: list[tuple]) -> list[tuple]:
    """events: (exchange_segment, security_id, ex_date, price_factor, volume_factor), applied ones only.
    Returns segment rows (seg, sec, valid_from, valid_to, cum_pf, cum_vf, n_events).
    A candle dated d is multiplied by the product of factors of events with ex_date > d."""
    by_inst: dict[tuple, dict[date, list[float]]] = {}
    for seg, sec, ex, pf, vf in events:
        d = by_inst.setdefault((seg, sec), {}).setdefault(ex, [1.0, 1.0, 0])
        d[0] *= pf
        d[1] *= vf
        d[2] += 1
    out = []
    for (seg, sec), dates in by_inst.items():
        ordered = sorted(dates)
        for i, boundary in enumerate(ordered):                          # segment ending at `boundary`
            later = ordered[i:]
            cum_pf = math.prod(dates[d][0] for d in later)
            cum_vf = math.prod(dates[d][1] for d in later)
            start = ordered[i - 1] if i else FAR_PAST
            out.append((seg, sec, start, boundary, cum_pf, cum_vf, sum(dates[d][2] for d in later)))
    return out


def _fingerprint(events: list[tuple]) -> str:
    key = "|".join(f"{e[0]}:{e[1]}:{e[2]}:{e[3]:.12g}:{e[4]:.12g}" for e in sorted(events, key=lambda e: (e[0], e[1], str(e[2]))))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def current_version(con) -> int:
    return con.execute("SELECT coalesce(max(version), 0) FROM adjustment_runs").fetchone()[0]


def apply_adjustments(con, tf) -> dict:
    """Rebuild adjustment_segments from verified events and (re)create the adjusted view, atomically.

    The adjustment VERSION only advances when the set of applied events (or any factor) actually changed, so repeating
    ``apply`` is a no-op and nothing that depends on the adjusted series (caches, exports, backtests) is invalidated for
    nothing. Each version is recorded with the exact events behind it. After a change, price continuity and volume scaling
    around every applied event are validated and the result stored (a failure is a flag for review, never silent)."""
    ev = con.execute("""SELECT exchange_segment, security_id, ex_date, price_factor, volume_factor, action_id
        FROM corporate_actions WHERE apply_adjustment AND price_factor IS NOT NULL AND security_id IS NOT NULL
        AND verification_status = 'verified' ORDER BY ex_date, action_id""").fetchall()
    events = [(a, b, c, d, e if e is not None else 1.0, f) for a, b, c, d, e, f in ev]
    segs, now = build_segments([x[:5] for x in events]), utcnow()
    fp = _fingerprint([x[:5] for x in events])
    last = con.execute("SELECT version, fingerprint FROM adjustment_runs ORDER BY version DESC LIMIT 1").fetchone()
    changed = last is None or last[1] != fp
    version = 1 if last is None else (last[0] + 1 if changed else last[0])
    con.begin()
    try:
        con.execute("DELETE FROM adjustment_segments")
        for s in segs:
            con.execute("INSERT INTO adjustment_segments VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [*s, now])
        key = f"CAST(c.{tf.key_col} AS DATE)" if tf.is_intraday else f"c.{tf.key_col}"
        con.execute(f"""CREATE OR REPLACE VIEW {tf.adjusted_view()} AS
            SELECT c.exchange_segment, c.security_id, c.{tf.key_col},
                   c.open * coalesce(s.cum_price_factor, 1.0) AS open,
                   c.high * coalesce(s.cum_price_factor, 1.0) AS high,
                   c.low * coalesce(s.cum_price_factor, 1.0) AS low,
                   c.close * coalesce(s.cum_price_factor, 1.0) AS close,
                   c.volume * coalesce(s.cum_volume_factor, 1.0) AS volume,
                   coalesce(s.cum_price_factor, 1.0) AS cum_price_factor,
                   coalesce(s.cum_volume_factor, 1.0) AS cum_volume_factor
            FROM {tf.table} c LEFT JOIN adjustment_segments s
              ON s.exchange_segment = c.exchange_segment AND s.security_id = c.security_id
             AND {key} >= s.valid_from AND {key} < s.valid_to""")
        if changed:
            con.execute("INSERT INTO adjustment_runs (version, applied_at_utc, events_applied, segments, fingerprint, event_ids) VALUES (?, ?, ?, ?, ?, ?)",
                        [version, now, len(events), len(segs), fp, ",".join(x[5] for x in events)])
            applied_ids = {x[5] for x in events}
            for aid, was in con.execute("SELECT action_id, application_status FROM corporate_actions").fetchall():
                if aid in applied_ids:
                    first = con.execute(f"SELECT min(CAST({tf.key_col} AS DATE)) FROM {tf.table} c WHERE exchange_segment = "
                                        "(SELECT exchange_segment FROM corporate_actions WHERE action_id = ?) AND security_id = "
                                        "(SELECT security_id FROM corporate_actions WHERE action_id = ?)", [aid, aid]).fetchone()[0]
                    con.execute("UPDATE corporate_actions SET application_status = 'applied', applied_version = ?, affected_from = ?, "
                                "affected_to = ex_date - INTERVAL 1 DAY WHERE action_id = ?", [version, first, aid])
                    if was != "applied":
                        revise(con, aid, "application", was, f"applied in adjustment v{version}", actor="apply")
                elif was == "applied":
                    con.execute("UPDATE corporate_actions SET application_status = 'withdrawn', affected_from = NULL, affected_to = NULL WHERE action_id = ?", [aid])
                    revise(con, aid, "application", was, f"withdrawn in adjustment v{version}", actor="apply")
        con.commit()
    except Exception:
        con.rollback()
        raise
    try:
        con.execute(f"COMMENT ON VIEW {tf.adjusted_view()} IS 'PRICE-adjusted only (verified split/bonus-type "
                    "events); no dividend or total-return adjustment. Raw data is in the base table.'")
    except Exception:  # comments are cosmetic
        pass
    failures = 0
    validations: list[dict] = []
    if events:
        validations = validate_applied(con, tf, version)
        failures = sum(1 for v in validations if not v["ok"])
        if changed:
            con.execute("UPDATE adjustment_runs SET validation_failures = ? WHERE version = ?", [failures, version])
    return {"events_applied": len(events), "segments": len(segs), "version": version, "changed": changed,
            "validation_failures": failures, "validations": validations}


GAP_TOLERANCE = (0.80, 1.25)        # an adjusted series should be continuous across the ex-date, within ordinary market moves
VOLUME_TOLERANCE = (0.25, 4.0)      # adjusted volume should scale across the ex-date: neither unchanged-raw nor vanished


def validate_applied(con, tf, version: int) -> list[dict]:
    """Price continuity and volume scaling around every applied event, on the ADJUSTED view (read-only apart from storing
    the result). A failed check does not undo the adjustment; it is recorded and shown for review."""
    out, now = [], utcnow()
    key = f"CAST({tf.key_col} AS DATE)" if tf.is_intraday else tf.key_col
    for aid, seg, sec, sym, ex in con.execute("""SELECT action_id, exchange_segment, security_id, symbol, ex_date FROM corporate_actions
            WHERE application_status = 'applied' AND applied_version = ? ORDER BY ex_date""", [version]).fetchall():
        sess = con.execute(
            f"""SELECT {key} AS d, arg_min(open, {tf.key_col}) AS open, arg_max(close, {tf.key_col}) AS close, sum(volume) AS volume
                FROM {tf.adjusted_view()} WHERE exchange_segment = ? AND security_id = ? AND {key} BETWEEN ? AND ? GROUP BY 1 ORDER BY 1""",
            [seg, sec, ex - timedelta(days=25), ex + timedelta(days=25)]).df()
        res = {"action_id": aid, "symbol": sym, "ex_date": ex, "ok": False, "detail": "no candles on both sides of the ex-date: cannot validate",
               "prev_session": None, "ex_session": None, "adj_prev_close": None, "ex_open": None, "gap_ratio": None, "adj_volume_ratio": None}
        if len(sess):
            sess["d"] = pd.to_datetime(sess["d"]).dt.date
            before, after = sess[sess["d"] < ex], sess[sess["d"] >= ex]
            if len(before) and len(after) and (after.iloc[0]["d"] - before.iloc[-1]["d"]).days <= MAX_SESSION_GAP_DAYS:
                prev, first = before.iloc[-1], after.iloc[0]
                gap = float(first["open"] / prev["close"]) if prev["close"] > 0 else None
                vb, va = before.tail(5)["volume"].median(), after.head(5)["volume"].median()
                vr = float(va / vb) if vb and va and vb > 0 and va > 0 else None
                price_ok = gap is not None and GAP_TOLERANCE[0] <= gap <= GAP_TOLERANCE[1]
                vol_ok = vr is None or VOLUME_TOLERANCE[0] <= vr <= VOLUME_TOLERANCE[1]
                res.update(prev_session=prev["d"], ex_session=first["d"], adj_prev_close=float(prev["close"]), ex_open=float(first["open"]),
                           gap_ratio=gap, adj_volume_ratio=vr, ok=bool(price_ok and vol_ok),
                           detail=("continuous across the ex-date" if price_ok and vol_ok else
                                   (f"price gap {gap:.3f} is outside the continuity band {GAP_TOLERANCE}" if not price_ok else "")
                                   + (f"; " if not price_ok and not vol_ok else "")
                                   + (f"adjusted volume ratio {vr:.2f} is outside {VOLUME_TOLERANCE}" if not vol_ok else "")))
        con.execute("""INSERT INTO adjustment_validations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (version, action_id) DO UPDATE SET checked_at_utc = excluded.checked_at_utc, ok = excluded.ok,
              prev_session = excluded.prev_session, ex_session = excluded.ex_session, adj_prev_close = excluded.adj_prev_close,
              ex_open = excluded.ex_open, gap_ratio = excluded.gap_ratio, adj_volume_ratio = excluded.adj_volume_ratio, detail = excluded.detail""",
                    [version, aid, now, res["ok"], res["prev_session"], res["ex_session"], res["adj_prev_close"], res["ex_open"],
                     res["gap_ratio"], res["adj_volume_ratio"], res["detail"]])
        out.append(res)
    return out


# ------------------------------------------------------------- adjusted series, with an as-of date (no look-ahead)
def verified_events(con, exchange_segment: str, security_id: int, *, as_of: date | None = None) -> list[tuple[date, float, float]]:
    """(ex_date, price_factor, volume_factor) of the VERIFIED, applicable events of one instrument, oldest first.
    ``as_of`` keeps only events whose ex-date is on or before that date: what a person standing on that day could know."""
    sql = ("SELECT ex_date, price_factor, coalesce(volume_factor, 1.0) FROM corporate_actions WHERE apply_adjustment "
           "AND verification_status = 'verified' AND price_factor IS NOT NULL AND exchange_segment = ? AND security_id = ?")
    args: list = [exchange_segment, security_id]
    if as_of is not None:
        sql += " AND ex_date <= ?"
        args.append(as_of)
    return [(a, float(b), float(c)) for a, b, c in con.execute(sql + " ORDER BY ex_date", args).fetchall()]


def adjust_frame(df: pd.DataFrame, key_col: str, events: list[tuple[date, float, float]]) -> pd.DataFrame:
    """Apply split/bonus factors to a candle frame (open/high/low/close/volume): a candle dated d is multiplied by the product
    of the factors of events with ex_date > d, exactly like the stored view. Pure: the input is not modified."""
    out = df.copy()
    out["cum_price_factor"], out["cum_volume_factor"] = 1.0, 1.0
    if not events or out.empty:
        return out
    day = pd.to_datetime(out[key_col]).dt.date
    pf = pd.Series(1.0, index=out.index)
    vf = pd.Series(1.0, index=out.index)
    for ex, p, v in events:
        mask = day < ex
        pf[mask] *= p
        vf[mask] *= v
    for c in ("open", "high", "low", "close"):
        out[c] = out[c] * pf
    out["volume"] = out["volume"] * vf
    out["cum_price_factor"], out["cum_volume_factor"] = pf, vf
    return out


def adjustment_summary(con) -> dict:
    """The state of the adjusted series in one database, for labels and the interface."""
    version = current_version(con)
    run = con.execute("SELECT applied_at_utc, events_applied, validation_failures FROM adjustment_runs WHERE version = ?", [version]).fetchone() if version else None
    counts = dict(con.execute("SELECT verification_status, count(*) FROM corporate_actions GROUP BY 1").fetchall())
    return {"version": version, "applied_at_utc": run[0] if run else None, "events_applied": run[1] if run else 0,
            "validation_failures": run[2] if run else 0, "events_by_status": counts,
            "unresolved": counts.get("unresolved", 0), "source_adjusted": counts.get("source_adjusted", 0),
            "proposed": counts.get("proposed", 0), "verified": counts.get("verified", 0),
            "basis_note": ("Adjusted candles are PRICE-adjusted for verified splits and bonus issues only: no dividend or total-return "
                           "adjustment, and no event that is unverified, unresolved or already adjusted by the source.")}


# ---------------------------------------------------------------------- gap scan
NICE_RATIOS = {0.5: "1:1 bonus or 2-for-1 split", 1 / 3: "2:1 bonus or 3-for-1 split", 0.25: "4-for-1 split / 3:1 bonus",
               0.2: "5-for-1 split / 4:1 bonus", 0.1: "10-for-1 split / 9:1 bonus", 2 / 3: "1:2 bonus",
               0.75: "1:3 bonus", 0.8: "1:4 bonus", 5 / 6: "1:5 bonus", 0.05: "20-for-1 split"}


def nearest_nice_ratio(ratio: float) -> str | None:
    best = min(NICE_RATIOS, key=lambda r: abs(math.log(ratio / r)))
    return f"{NICE_RATIOS[best]} ({best:.3f})" if abs(math.log(ratio / best)) < 0.04 else None


def scan_gaps(con, tf, *, min_log_gap: float = 0.15, max_days: int = 10) -> pd.DataFrame:
    """Overnight gaps that look like unrecorded splits/bonuses, with the matching recorded event if any."""
    if tf.is_intraday:
        sess = (f"SELECT exchange_segment, security_id, CAST({tf.key_col} AS DATE) AS d, "
                f"arg_min(open, {tf.key_col}) AS open, arg_max(close, {tf.key_col}) AS close, sum(volume) AS volume "
                f"FROM {tf.table} GROUP BY 1, 2, 3")
    else:
        sess = f"SELECT exchange_segment, security_id, {tf.key_col} AS d, open, close, volume FROM {tf.table}"
    df = con.execute(f"""WITH s AS ({sess}), l AS (
          SELECT *, lag(close) OVER w AS prev_close, lag(d) OVER w AS prev_d,
                 median(volume) OVER (PARTITION BY exchange_segment, security_id ORDER BY d ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS vb,
                 median(volume) OVER (PARTITION BY exchange_segment, security_id ORDER BY d ROWS BETWEEN CURRENT ROW AND 4 FOLLOWING) AS va
          FROM s WINDOW w AS (PARTITION BY exchange_segment, security_id ORDER BY d))
        SELECT exchange_segment, security_id, d AS event_date, prev_close, open AS next_open,
               open / prev_close AS ratio, CASE WHEN vb > 0 THEN va / vb END AS volume_ratio
        FROM l WHERE prev_close > 0 AND date_diff('day', prev_d, d) <= {max_days}
          AND abs(ln(open / prev_close)) >= {min_log_gap} ORDER BY exchange_segment, security_id, d""").df()
    if df.empty:
        return df.assign(nearest_ratio=[], matched_action_id=[])
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
    df["nearest_ratio"] = df["ratio"].map(lambda r: nearest_nice_ratio(float(r)))
    matched = []
    for r in df.itertuples():
        m = con.execute("""SELECT action_id FROM corporate_actions WHERE security_id = ? AND exchange_segment = ?
                           AND abs(date_diff('day', ex_date, ?)) <= 3 LIMIT 1""",
                        [r.security_id, r.exchange_segment, r.event_date]).fetchone()
        matched.append(m[0] if m else None)
    nearest = [None if pd.isna(v) else v for v in df["nearest_ratio"]]
    df["nearest_ratio"] = pd.Series(nearest, dtype=object, index=df.index)
    df["matched_action_id"] = pd.Series(matched, dtype=object, index=df.index)   # real None, never NaN
    now = utcnow()
    for r, near, match in zip(df.itertuples(), nearest, matched):
        con.execute("""INSERT INTO adjustment_scan_findings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (exchange_segment, security_id, event_date) DO UPDATE SET prev_close = excluded.prev_close,
              next_open = excluded.next_open, ratio = excluded.ratio, volume_ratio = excluded.volume_ratio,
              nearest_ratio = excluded.nearest_ratio, matched_action_id = excluded.matched_action_id,
              found_at_utc = excluded.found_at_utc""",
                    [r.exchange_segment, r.security_id, r.event_date, r.prev_close, r.next_open, r.ratio,
                     None if pd.isna(r.volume_ratio) else r.volume_ratio, near, match, now])
    return df


# --------------------------------------------------------------- cross-timeframe check
def crosscheck(daily_con, intraday_con) -> pd.DataFrame:
    """Read-only comparison of the two databases around each event: are both on the same
    (raw/adjusted) basis, and do 15-minute sessions agree with daily candles?"""
    ev = daily_con.execute("""SELECT a.action_id, a.symbol, a.event_type, a.ex_date, a.security_id, c.basis AS daily_basis,
        c.prev_session FROM corporate_actions a LEFT JOIN corporate_action_checks c USING (action_id)
        WHERE a.adjusts_price ORDER BY a.symbol, a.ex_date""").fetchall()
    rows = []
    for aid, sym, etype, ex, sec, d_basis, prev in ev:
        i_basis = intraday_con.execute("SELECT basis FROM corporate_action_checks WHERE action_id = ?", [aid]).fetchone()
        i_basis = i_basis[0] if i_basis else None
        close_ratio = vol_ratio = None
        if prev is not None and sec is not None:
            d = daily_con.execute("SELECT close, volume FROM candles_daily WHERE security_id = ? AND candle_date = ?",
                                  [sec, prev]).fetchone()
            i = intraday_con.execute("SELECT arg_max(close, candle_timestamp), sum(volume) FROM candles_15min "
                                     "WHERE security_id = ? AND CAST(candle_timestamp AS DATE) = ?", [sec, prev]).fetchone()
            if d and i and i[0] and d[0]:
                close_ratio = i[0] / d[0]
                vol_ratio = (i[1] / d[1]) if d[1] else None
        same = d_basis == i_basis and d_basis in ("unadjusted", "adjusted")
        rows.append({"symbol": sym, "type": etype, "ex_date": ex, "daily_basis": d_basis, "15min_basis": i_basis,
                     "same_basis": same, "15min_last_close/daily_close": close_ratio,
                     "15min_volume/daily_volume": vol_ratio})
    return pd.DataFrame(rows)
