"""Corporate-action methodology on SYNTHETIC series (no real Dhan/NSE data was available)."""
import math
import random
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from conftest import load_universe
from stockdata import corp_actions as ca
from stockdata.db import CandleDB
from stockdata.instruments import Instrument
from stockdata.timeframes import DAILY, INTRADAY_15
from stockdata.windows import Window

EX = date(2024, 3, 12)
START, END = date(2024, 2, 19), date(2024, 5, 10)


def weekdays(a, b):
    d = a
    while d < b:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def bars(factor, basis, ex, *, base=1000.0, vol=100_000, seed=1):
    """Per-session lists of 25 synthetic 15-min bars. basis: 'raw' (jump at ex) | 'adjusted' (continuous)."""
    rng, out = random.Random(seed), {}
    for i, d in enumerate(weekdays(START, END)):
        true = base * (1 + 0.002 * i)
        mult = factor if d >= ex else (1.0 if basis == "raw" else factor)
        v = vol if basis == "raw" and d < ex else (vol / factor if basis == "adjusted" or d >= ex else vol)
        day, price = [], true * mult
        for slot in range(25):
            o = price
            c = o * (1 + rng.uniform(-0.002, 0.002))
            day.append((datetime.combine(d, time(9, 15)) + timedelta(minutes=15 * slot), o,
                        max(o, c) * 1.001, min(o, c) * 0.999, c, int(v / 25)))
            price = c
        out[d] = day
    return out


def put(db, security_id, sessions):
    tf = db.tf
    rows = []
    for d, day in sessions.items():
        if tf.is_intraday:
            rows += [(ts, o, h, l, c, v) for ts, o, h, l, c, v in day]
        else:
            rows.append((datetime.combine(d, time.min), day[0][1], max(b[2] for b in day),
                         min(b[3] for b in day), day[-1][4], sum(b[5] for b in day)))
    df = pd.DataFrame(rows, columns=[tf.key_col, "open", "high", "low", "close", "volume"])
    df[tf.key_col] = df[tf.key_col].astype("datetime64[us]")
    df["epoch_raw"] = 0
    inst = Instrument("NSE_EQ", security_id, "X", "EQUITY")
    empty = pd.DataFrame({"candle_key": pd.Series([], dtype="datetime64[us]"), "kind": [], "detail": []})
    db.commit_window(inst, Window(datetime.combine(START, time.min), datetime.combine(END, time.min)),
                     candles=df, anomalies=empty, zero_volume=0, attempts=1, http_status=200)


@pytest.fixture(params=["daily", "15min"])
def cadb(request, tmp_path):
    tf = DAILY if request.param == "daily" else INTRADAY_15
    with CandleDB(tf, tmp_path / tf.db_name) as db:
        load_universe(db)
        # 900001 SIMALPHA: 1:1 bonus, history is RAW   | 900002 SIMBETA: 10->2 split, history already ADJUSTED
        # 900003 SIMGAMMA: 1:1 bonus, RAW but the source record is unconfirmed
        put(db, 900001, bars(0.5, "raw", EX, seed=1))
        put(db, 900002, bars(0.2, "adjusted", date(2024, 4, 15), base=800.0, seed=2))
        put(db, 900003, bars(0.5, "raw", EX, base=300.0, seed=3))
        yield db


def rows_for(sym, etype, ex, **kw):
    return {"symbol": sym, "event_type": etype, "ex_date": ex.isoformat(), "ratio_text": kw.pop("ratio_text", ""), **kw}


def seed_events(db, *, confirmed=True):
    rows = [rows_for("SIMALPHA", "BONUS", EX, ratio_text="Bonus 1:1", bonus_new=1, bonus_held=1),
            rows_for("SIMBETA", "SPLIT", date(2024, 4, 15), ratio_text="Split 10 to 2", old_face_value=10, new_face_value=2),
            rows_for("SIMALPHA", "DIVIDEND", date(2024, 4, 1), ratio_text="Dividend Rs 5", cash_amount=5),
            rows_for("SIMALPHA", "RIGHTS", date(2024, 4, 22), ratio_text="Rights 1:4 @ 80", rights_new=1, rights_held=4,
                     issue_price=80),
            rows_for("SIMBETA", "MERGER", date(2024, 4, 30), ratio_text="Amalgamation"),
            rows_for("SIMGAMMA", "SPLIT", date(2024, 5, 1), ratio_text="Split, ratio unknown"),
            rows_for("NOSUCH", "BONUS", EX, ratio_text="Bonus 1:1", bonus_new=1, bonus_held=1)]
    ca.import_actions(db.con, rows, source="NSE corporate actions", source_ref="synthetic test", source_confirmed=confirmed)
    ca.import_actions(db.con, [rows_for("SIMGAMMA", "BONUS", EX, ratio_text="Bonus 1:1", bonus_new=1, bonus_held=1)],
                      source="company filing", source_ref="synthetic test", source_confirmed=False)


def status(db):
    return {(r[0], r[1]): r[2:] for r in db.con.execute(
        "SELECT symbol, event_type, verification_status, apply_adjustment, price_factor FROM corporate_actions").fetchall()}


# ------------------------------------------------------------------------------ formulas
def test_factor_formulas():
    assert ca.split_factors(10, 2) == pytest.approx((0.2, 5.0))
    assert ca.split_factors(1, 10) == pytest.approx((10.0, 0.1))                  # consolidation
    assert ca.bonus_factors(1, 1) == pytest.approx((0.5, 2.0))
    assert ca.bonus_factors(1, 2) == pytest.approx((2 / 3, 1.5))                  # 1 new for every 2 held
    assert ca.bonus_factors(2, 1) == pytest.approx((1 / 3, 3.0))
    assert ca.rights_terp_factor(100, 80, 1, 4) == pytest.approx(0.96)            # TERP = (4*100 + 80)/5 = 96
    for bad in ((0, 1), (1, 0)):
        with pytest.raises(ValueError):
            ca.bonus_factors(*bad)


@pytest.mark.parametrize("text,fv,expect", [
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", None,
     {"event_type": "SPLIT", "old_face_value": 10.0, "new_face_value": 1.0}),
    ("Bonus 1:1", None, {"event_type": "BONUS", "bonus_new": 1.0, "bonus_held": 1.0}),
    ("Bonus issue 3:2", None, {"event_type": "BONUS", "bonus_new": 3.0, "bonus_held": 2.0}),
    ("Rights 1:5 @ Premium Rs 120/-", 10.0, {"event_type": "RIGHTS", "rights_new": 1.0, "rights_held": 5.0, "issue_price": 130.0}),
    ("Dividend - Rs 5.50 Per Share", None, {"event_type": "DIVIDEND", "cash_amount": 5.5}),
    ("Dividend - 250%", 10.0, {"event_type": "DIVIDEND", "cash_amount": 25.0}),
    ("Scheme Of Arrangement - Demerger", None, {"event_type": "DEMERGER"}),
    ("Amalgamation", None, {"event_type": "MERGER"}),
    ("Bonus 1:1 And Dividend Rs 2", None, {"event_type": "OTHER"}),              # several kinds -> never guessed
    ("AGM", None, {"event_type": "OTHER"})])
def test_nse_purpose_parser_is_conservative(text, fv, expect):
    got = ca.parse_nse_purpose(text, fv)
    assert {k: got.get(k) for k in expect} == expect


def test_unparseable_split_ratio_is_unresolved_not_guessed(cadb):
    res = ca.import_actions(cadb.con, [rows_for("SIMALPHA", "SPLIT", EX, ratio_text="Split, ratio unknown")],
                            source="s", source_ref="r", source_confirmed=True)
    assert res.by_status == {"unresolved": 1}
    assert cadb.con.execute("SELECT price_factor FROM corporate_actions").fetchone()[0] is None


# ----------------------------------------------------------------------- import behaviour
def test_import_statuses_and_idempotency(cadb):
    seed_events(cadb)
    st = status(cadb)
    assert st[("SIMALPHA", "BONUS")][0] == "proposed" and st[("SIMALPHA", "BONUS")][2] == 0.5
    assert st[("SIMBETA", "SPLIT")][2] == pytest.approx(0.2)
    assert st[("SIMALPHA", "DIVIDEND")][0] == "recorded_not_adjusted"
    assert st[("SIMALPHA", "RIGHTS")][0] == "unresolved" and st[("SIMBETA", "MERGER")][0] == "unresolved"
    assert st[("NOSUCH", "BONUS")][0] == "unresolved"
    assert not any(v[1] for v in st.values())                         # nothing applied before evaluation
    before = cadb.con.execute("SELECT * FROM corporate_actions ORDER BY action_id").fetchall()
    seed_events(cadb)                                                 # re-import: nothing duplicated or reset
    assert cadb.con.execute("SELECT * FROM corporate_actions ORDER BY action_id").fetchall() == before


# -------------------------------------------------------------------------- classification
@pytest.mark.parametrize("obs,f,vr,vf,expect", [
    (0.50, 0.5, 2.0, 2.0, "unadjusted"), (0.52, 0.5, 1.8, 2.0, "unadjusted"),
    (1.00, 0.5, 1.0, 2.0, "adjusted"), (0.98, 0.5, 1.1, 2.0, "adjusted"),
    (0.75, 0.5, None, None, "ambiguous"),                   # matches neither
    (0.92, 0.9, None, None, "ambiguous"),                   # factor too close to 1 to separate from normal moves
    (0.50, 0.5, 1.0, 2.0, "ambiguous"),                     # price says raw but volume did not rise
    (1.00, 0.5, 2.0, 2.0, "ambiguous")])                    # price says adjusted but volume jumped
def test_classify_basis(obs, f, vr, vf, expect):
    assert ca.classify_basis(obs, f, vr, vf)[0] == expect


def test_independent_raw_close_is_decisive():
    assert ca.classify_basis(None, 0.5, None, None, ref_ratio=1.0)[0] == "unadjusted"
    assert ca.classify_basis(None, 0.5, None, None, ref_ratio=0.5)[0] == "adjusted"
    assert ca.classify_basis(None, 0.5, None, None, ref_ratio=0.75)[0] == "ambiguous"


# ---------------------------------------------------------------------------- evaluation
def test_evaluate_marks_raw_adjusted_unconfirmed_and_unresolved(cadb):
    seed_events(cadb)
    ca.evaluate_all(cadb.con, cadb.tf)
    st = status(cadb)
    assert st[("SIMALPHA", "BONUS")][:2] == ("verified", True)                  # raw jump + confirmed source
    assert st[("SIMBETA", "SPLIT")][:2] == ("source_adjusted", False)           # already adjusted: left alone
    gamma = cadb.con.execute("SELECT verification_status, apply_adjustment FROM corporate_actions "
                             "WHERE symbol = 'SIMGAMMA' AND event_type = 'BONUS'").fetchone()
    assert gamma == ("proposed", False)                                         # raw jump but source unconfirmed
    assert st[("SIMGAMMA", "SPLIT")][:2] == ("unresolved", False)               # no ratio -> never guessed
    assert st[("SIMALPHA", "RIGHTS")][:2] == ("unresolved", False)
    assert st[("SIMALPHA", "DIVIDEND")][0] == "recorded_not_adjusted"
    chk = cadb.con.execute("SELECT basis, observed_ratio FROM corporate_action_checks c JOIN corporate_actions a "
                           "USING (action_id) WHERE a.symbol = 'SIMALPHA' AND a.event_type = 'BONUS'").fetchone()
    assert chk[0] == "unadjusted" and chk[1] == pytest.approx(0.5, rel=0.02)


def test_already_adjusted_history_is_never_adjusted_again(cadb):
    seed_events(cadb)
    ca.evaluate_all(cadb.con, cadb.tf)
    beta = cadb.con.execute("""SELECT verification_status, apply_adjustment, c.basis FROM corporate_actions a
        JOIN corporate_action_checks c USING (action_id) WHERE a.symbol = 'SIMBETA' AND a.event_type = 'SPLIT'
        AND a.price_factor IS NOT NULL""").fetchone()
    assert beta == ("source_adjusted", False, "adjusted")
    raw = cadb.con.execute(f"SELECT * FROM {cadb.tf.table} WHERE security_id = 900002 ORDER BY ALL").fetchall()
    ca.apply_adjustments(cadb.con, cadb.tf)
    adj = cadb.con.execute(f"SELECT exchange_segment, security_id, {cadb.tf.key_col}, open, high, low, close, volume "
                           f"FROM {cadb.tf.adjusted_view()} WHERE security_id = 900002 ORDER BY ALL").fetchall()
    assert [r[3:] for r in adj] == [(r[3], r[4], r[5], r[6], r[7]) for r in raw]  # identical to raw


def test_evaluation_with_an_independent_raw_close(cadb):
    seed_events(cadb)
    sess = ca.sessions_around(cadb.con, cadb.tf, "NSE_EQ", 900001, EX)
    prev = sess[pd.to_datetime(sess["d"]).dt.date < EX].iloc[-1]
    prev_date, prev_close = pd.to_datetime(prev["d"]).date(), float(prev["close"])
    ca.evaluate_all(cadb.con, cadb.tf, reference={("SIMALPHA", prev_date): prev_close})   # NSE raw close == Dhan
    assert "pre-ex close equals the independent raw close" in cadb.con.execute(
        "SELECT detail FROM corporate_action_checks c JOIN corporate_actions a USING (action_id) "
        "WHERE a.symbol = 'SIMALPHA' AND a.event_type = 'BONUS'").fetchone()[0]


# --------------------------------------------------------------------------- adjusted data
def test_adjusted_view_is_correct_and_changes_nothing_else(cadb):
    seed_events(cadb)
    ca.evaluate_all(cadb.con, cadb.tf)
    tf, k = cadb.tf, cadb.tf.key_col
    raw = cadb.con.execute(f"SELECT * FROM {tf.table} ORDER BY ALL").fetchall()
    info = ca.apply_adjustments(cadb.con, tf)
    assert info["events_applied"] == 1
    assert cadb.con.execute(f"SELECT * FROM {tf.table} ORDER BY ALL").fetchall() == raw        # raw preserved
    n_raw = len(raw)
    assert cadb.con.execute(f"SELECT count(*) FROM {tf.adjusted_view()}").fetchone()[0] == n_raw   # no new/dup bars
    key = f"CAST(r.{k} AS DATE)"
    pre = cadb.con.execute(f"""SELECT count(*), max(abs(a.close / r.close - 0.5)), max(abs(a.volume / r.volume - 2.0)),
        max(abs(a.open / r.open - 0.5)), max(abs(a.high / r.high - 0.5)), max(abs(a.low / r.low - 0.5))
        FROM {tf.table} r JOIN {tf.adjusted_view()} a USING (exchange_segment, security_id, {k})
        WHERE r.security_id = 900001 AND {key} < DATE '{EX}'""").fetchone()
    assert pre[0] > 0 and max(pre[1:]) < 1e-9                                  # before ex-date: x0.5 price, x2 volume
    same = cadb.con.execute(f"""SELECT count(*) FROM {tf.table} r JOIN {tf.adjusted_view()} a
        USING (exchange_segment, security_id, {k}) WHERE (r.security_id <> 900001 OR {key} >= DATE '{EX}')
        AND (a.close <> r.close OR a.open <> r.open OR a.high <> r.high OR a.low <> r.low OR a.volume <> r.volume)""").fetchone()[0]
    assert same == 0                                                            # ex-date onwards and other stocks untouched
    # the adjusted series is continuous across the ex-date (that was the point of adjusting)
    s = cadb.con.execute(f"""SELECT {key.replace('r.', '')} AS d, arg_min(open, {k}) o, arg_max(close, {k}) c
        FROM {tf.adjusted_view()} WHERE security_id = 900001 GROUP BY 1 ORDER BY 1""").df() if tf.is_intraday else \
        cadb.con.execute(f"SELECT {k} AS d, open AS o, close AS c FROM {tf.adjusted_view()} WHERE security_id = 900001 ORDER BY 1").df()
    s["d"] = pd.to_datetime(s["d"]).dt.date
    gap = s[s["d"] >= EX].iloc[0]["o"] / s[s["d"] < EX].iloc[-1]["c"]
    assert gap == pytest.approx(1.0, abs=0.02)


def test_apply_is_idempotent_and_adjusted_timeframes_agree(tmp_path):
    out = {}
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, tmp_path / tf.db_name) as db:
            load_universe(db)
            put(db, 900001, bars(0.5, "raw", EX, seed=1))
            seed_events(db)
            ca.evaluate_all(db.con, tf)
            ca.apply_adjustments(db.con, tf)
            first = db.con.execute(f"SELECT * FROM {tf.adjusted_view()} ORDER BY ALL").fetchall()
            ca.apply_adjustments(db.con, tf)
            assert db.con.execute(f"SELECT * FROM {tf.adjusted_view()} ORDER BY ALL").fetchall() == first
            out[tf.name] = db.con.execute(
                f"""SELECT CAST({tf.key_col} AS DATE) d, sum(volume) v, arg_max(close, {tf.key_col}) c
                    FROM {tf.adjusted_view()} WHERE security_id = 900001 GROUP BY 1 ORDER BY 1""").df()
    d, i = out["daily"], out["15min"]
    assert len(d) == len(i)
    assert (abs(d["c"] - i["c"]) < 1e-9).all()                  # daily and 15-min adjusted closes agree session by session
    assert (abs(d["v"] - i["v"]) < 1e-6).all()                  # ... and so do adjusted volumes


def test_segments_for_several_events_multiply_correctly():
    seg = ca.build_segments([("NSE_EQ", 1, date(2024, 1, 10), 0.5, 2.0), ("NSE_EQ", 1, date(2024, 6, 10), 0.2, 5.0),
                             ("NSE_EQ", 2, date(2024, 3, 1), 0.8, 1.25)])
    one = sorted(s for s in seg if s[1] == 1)
    assert one[0][2:4] == (ca.FAR_PAST, date(2024, 1, 10)) and one[0][4] == pytest.approx(0.1) and one[0][5] == pytest.approx(10)
    assert one[1][2:4] == (date(2024, 1, 10), date(2024, 6, 10)) and one[1][4] == pytest.approx(0.2)
    assert len(one) == 2                                         # after the last event the factor is 1: no row needed
    assert [s for s in seg if s[1] == 2][0][4] == pytest.approx(0.8)


def test_same_day_events_combine():
    seg = ca.build_segments([("NSE_EQ", 1, date(2024, 1, 10), 0.5, 2.0), ("NSE_EQ", 1, date(2024, 1, 10), 0.2, 5.0)])
    assert len(seg) == 1 and seg[0][4] == pytest.approx(0.1) and seg[0][6] == 2


# --------------------------------------------------------------------- manual verification
def test_manual_verification_is_explicit_audited_and_required_for_rights(cadb):
    seed_events(cadb)
    ca.evaluate_all(cadb.con, cadb.tf)
    rid = cadb.con.execute("SELECT action_id FROM corporate_actions WHERE event_type = 'RIGHTS'").fetchone()[0]
    with pytest.raises(ValueError):
        ca.set_manual_status(cadb.con, rid, "verified", "  ")                   # a note is mandatory
    ca.set_manual_status(cadb.con, rid, "verified", "factor taken from NSE circular dated 2024-04-19")
    row = cadb.con.execute("SELECT verification_status, apply_adjustment, verification_note FROM corporate_actions "
                           "WHERE action_id = ?", [rid]).fetchone()
    assert row[:2] == ("verified", True) and row[2].startswith("manual:")
    ca.evaluate_all(cadb.con, cadb.tf)                                           # re-evaluation must not override the human decision
    assert cadb.con.execute("SELECT verification_status FROM corporate_actions WHERE action_id = ?", [rid]).fetchone()[0] == "verified"


def test_dividends_never_enter_the_adjusted_series(cadb):
    seed_events(cadb)
    ca.evaluate_all(cadb.con, cadb.tf)
    ca.apply_adjustments(cadb.con, cadb.tf)
    applied = cadb.con.execute("SELECT event_type FROM corporate_actions WHERE apply_adjustment").fetchall()
    assert applied == [("BONUS",)]


# ---------------------------------------------------------------------------------- scan
def test_gap_scan_finds_unrecorded_and_matches_recorded_events(cadb):
    ca.import_actions(cadb.con, [rows_for("SIMALPHA", "BONUS", EX, ratio_text="Bonus 1:1", bonus_new=1, bonus_held=1)],
                      source="s", source_ref="r", source_confirmed=True)
    df = cadb.con.execute("SELECT 1").fetchone() and ca.scan_gaps(cadb.con, cadb.tf)
    found = {(int(r.security_id), r.event_date): r for r in df.itertuples()}
    a = found[(900001, EX)]
    assert a.ratio == pytest.approx(0.5, rel=0.02) and a.matched_action_id is not None and "1:1 bonus" in a.nearest_ratio
    g = found[(900003, EX)]
    assert g.matched_action_id is None                              # a gap with no recorded event -> flagged for review
    assert (900002, date(2024, 4, 15)) not in found                 # adjusted history has no gap
    stored = cadb.con.execute("SELECT security_id, matched_action_id, nearest_ratio FROM adjustment_scan_findings "
                              "ORDER BY security_id").fetchall()
    assert [(s[0], s[1] is not None) for s in stored] == [(900001, True), (900003, False)]   # real NULL, not 'nan'


# ----------------------------------------------------------------------- cross-timeframe
def test_crosscheck_reports_same_basis_in_both_databases(tmp_path):
    dbs = {}
    for tf in (DAILY, INTRADAY_15):
        db = CandleDB(tf, tmp_path / tf.db_name)
        load_universe(db)
        put(db, 900001, bars(0.5, "raw", EX, seed=1))
        seed_events(db)
        ca.evaluate_all(db.con, tf)
        dbs[tf.name] = db
    res = ca.crosscheck(dbs["daily"].con, dbs["15min"].con)
    a = res[(res["symbol"] == "SIMALPHA") & (res["type"] == "BONUS")].iloc[0]
    assert a["same_basis"] and a["daily_basis"] == "unadjusted"
    assert a["15min_last_close/daily_close"] == pytest.approx(1.0) and a["15min_volume/daily_volume"] == pytest.approx(1.0, rel=1e-3)
    for db in dbs.values():
        db.close()


def test_nse_csv_import_end_to_end(cadb):
    df = pd.DataFrame({"SYMBOL": ["SIMALPHA", "SIMALPHA"], "COMPANY NAME": ["x", "x"], "SERIES": ["EQ", "EQ"],
                       "PURPOSE": ["Bonus 1:1", "Dividend - Rs 5 Per Share"], "FACE VALUE": ["10", "10"],
                       "EX-DATE": ["12-Mar-2024", "01-Apr-2024"], "RECORD DATE": ["13-Mar-2024", "02-Apr-2024"]})
    res = ca.import_actions(cadb.con, ca.rows_from_nse_csv(df), source="NSE", source_ref="synthetic", source_confirmed=True)
    assert res.inserted == 2 and res.by_status == {"proposed": 1, "recorded_not_adjusted": 1}
    with pytest.raises(ValueError):
        ca.rows_from_nse_csv(pd.DataFrame({"FOO": ["1"]}))
