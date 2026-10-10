"""The corporate-action ledger as an audit trail: revisions, evidence, versioned application, validation around the event,
as-of (look-ahead-free) adjusted series, provider mixing. Synthetic series only."""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from conftest import load_universe
from stockdata import corp_actions as ca
from stockdata.db import CandleDB
from stockdata.timeframes import DAILY, INTRADAY_15
from test_corp_actions import EX, bars, cadb, put, rows_for, seed_events, status  # noqa: F401


def revisions(db, symbol, etype):
    aid = db.con.execute("SELECT action_id FROM corporate_actions WHERE symbol = ? AND event_type = ?", [symbol, etype]).fetchone()[0]
    return aid, db.con.execute("SELECT version, actor, change, old_value, new_value, note FROM corporate_action_revisions WHERE action_id = ? "
                               "ORDER BY revision_id", [aid]).fetchall()


def test_every_change_to_an_event_leaves_a_revision_and_reimporting_changes_nothing(cadb):
    seed_events(cadb)
    aid, revs = revisions(cadb, "SIMALPHA", "BONUS")
    assert [(r[0], r[2]) for r in revs] == [(1, "created")] and "synthetic test" in revs[0][5]
    ca.evaluate_all(cadb.con, cadb.tf)
    _, revs = revisions(cadb, "SIMALPHA", "BONUS")
    assert [r[2] for r in revs] == ["created", "verification"] and revs[1][3].startswith("proposed") and revs[1][4].startswith("verified/apply")
    assert cadb.con.execute("SELECT version FROM corporate_actions WHERE action_id = ?", [aid]).fetchone()[0] == 2
    ca.evaluate_all(cadb.con, cadb.tf)                                                      # nothing changed: no new revision
    assert len(revisions(cadb, "SIMALPHA", "BONUS")[1]) == 2
    before = cadb.con.execute("SELECT count(*) FROM corporate_action_revisions").fetchone()[0]
    ca.import_actions(cadb.con, [rows_for("SIMALPHA", "BONUS", EX, ratio_text="Bonus 1:1", bonus_new=1, bonus_held=1)],
                      source="NSE corporate actions", source_ref="synthetic test", source_confirmed=True)
    assert cadb.con.execute("SELECT count(*) FROM corporate_action_revisions").fetchone()[0] == before


def test_a_manual_decision_is_a_revision_naming_who_decided_and_why(cadb):
    seed_events(cadb)
    ca.evaluate_all(cadb.con, cadb.tf)
    rid = cadb.con.execute("SELECT action_id FROM corporate_actions WHERE event_type = 'RIGHTS'").fetchone()[0]
    ca.set_manual_status(cadb.con, rid, "verified", "NSE circular NSE/CML/2024/10 confirms the TERP factor")
    _, revs = revisions(cadb, "SIMALPHA", "RIGHTS")
    last = revs[-1]
    assert last[1] == "person" and last[2] == "manual_decision" and "NSE/CML/2024/10" in last[5] and last[4].startswith("verified")


def test_the_ledger_keeps_the_evidence_the_source_and_when_it_was_retrieved(cadb):
    ca.import_actions(cadb.con, [rows_for("SIMALPHA", "BONUS", EX, ratio_text="Bonus 1:1", bonus_new=1, bonus_held=1,
                                          source_url="https://example.invalid/circular/123", evidence="Company filing dated 2024-02-01, clause 4",
                                          announcement_date="2024-02-01", record_date=EX.isoformat())],
                      source="company filing", source_ref="circular 123", source_confirmed=True)
    row = cadb.con.execute("SELECT source, source_ref, source_url, evidence, retrieved_at_utc, announcement_date, application_status, version "
                           "FROM corporate_actions").fetchone()
    assert row[0] == "company filing" and row[2].endswith("/123") and "clause 4" in row[3] and row[4] is not None
    assert row[5] == date(2024, 2, 1) and row[6] == "not_applied" and row[7] == 1


def test_application_is_versioned_idempotent_and_records_the_affected_range(cadb):
    seed_events(cadb)
    ca.evaluate_all(cadb.con, cadb.tf)
    first = ca.apply_adjustments(cadb.con, cadb.tf)
    assert first["version"] == 1 and first["changed"] is True and first["events_applied"] == 1
    aid, _ = revisions(cadb, "SIMALPHA", "BONUS")
    row = cadb.con.execute("SELECT application_status, applied_version, affected_from, affected_to FROM corporate_actions WHERE action_id = ?", [aid]).fetchone()
    assert row[0] == "applied" and row[1] == 1 and row[3] == EX - timedelta(days=1) and row[2] < EX      # strictly before the ex-date
    again = ca.apply_adjustments(cadb.con, cadb.tf)
    assert again["version"] == 1 and again["changed"] is False                                        # nothing changed: nothing invalidated
    assert cadb.con.execute("SELECT count(*) FROM adjustment_runs").fetchone()[0] == 1
    assert cadb.con.execute("SELECT event_ids FROM adjustment_runs WHERE version = 1").fetchone()[0] == aid
    # a second verified event advances the version; withdrawing one does too, and says so
    sid = cadb.con.execute("SELECT action_id FROM corporate_actions WHERE symbol = 'SIMBETA' AND event_type = 'SPLIT'").fetchone()[0]
    ca.set_manual_status(cadb.con, sid, "verified", "NSE circular confirms 10:2 split; candles are raw in this fixture")
    assert ca.apply_adjustments(cadb.con, cadb.tf)["version"] == 2
    ca.set_manual_status(cadb.con, sid, "rejected", "the circular was withdrawn")
    third = ca.apply_adjustments(cadb.con, cadb.tf)
    assert third["version"] == 3 and cadb.con.execute("SELECT application_status FROM corporate_actions WHERE action_id = ?", [sid]).fetchone()[0] == "withdrawn"
    _, revs = revisions(cadb, "SIMBETA", "SPLIT")
    assert any(r[2] == "application" and "withdrawn" in r[4] for r in revs)
    assert ca.adjustment_summary(cadb.con)["version"] == 3


def test_price_continuity_and_volume_scaling_are_validated_after_applying(cadb):
    seed_events(cadb)
    ca.evaluate_all(cadb.con, cadb.tf)
    out = ca.apply_adjustments(cadb.con, cadb.tf)
    assert out["validation_failures"] == 0 and len(out["validations"]) == 1
    v = out["validations"][0]
    assert v["ok"] and v["detail"] == "continuous across the ex-date" and v["gap_ratio"] == pytest.approx(1.0, abs=0.05)
    assert v["adj_volume_ratio"] == pytest.approx(1.0, abs=0.3) and v["ex_session"] >= EX and v["prev_session"] < EX
    assert cadb.con.execute("SELECT validation_failures FROM adjustment_runs WHERE version = 1").fetchone()[0] == 0


def test_a_wrong_factor_confirmed_by_a_person_is_applied_but_flagged_by_the_validation(cadb):
    # the candles are RAW and halve at the ex-date; someone enters a 0.9 factor and verifies it: the adjusted series is NOT continuous
    ca.import_actions(cadb.con, [rows_for("SIMALPHA", "SPLIT", EX, ratio_text="mistyped", price_factor_override=0.9, volume_factor_override=1.1111)],
                      source="manual entry", source_ref="typo", source_confirmed=True)
    aid = cadb.con.execute("SELECT action_id FROM corporate_actions").fetchone()[0]
    ca.set_manual_status(cadb.con, aid, "verified", "entered by hand for the test")
    out = ca.apply_adjustments(cadb.con, cadb.tf)
    assert out["events_applied"] == 1 and out["validation_failures"] == 1
    bad = out["validations"][0]
    assert bad["ok"] is False and "outside the continuity band" in bad["detail"]
    assert cadb.con.execute("SELECT ok FROM adjustment_validations WHERE action_id = ?", [aid]).fetchone()[0] is False


def test_the_adjusted_frame_matches_the_stored_view_and_respects_an_as_of_date(cadb):
    seed_events(cadb)
    ca.evaluate_all(cadb.con, cadb.tf)
    ca.apply_adjustments(cadb.con, cadb.tf)
    tf, k = cadb.tf, cadb.tf.key_col
    raw = cadb.con.execute(f"SELECT {k}, open, high, low, close, volume FROM {tf.table} WHERE security_id = 900001 ORDER BY {k}").df()
    view = cadb.con.execute(f"SELECT {k}, open, high, low, close, volume FROM {tf.adjusted_view()} WHERE security_id = 900001 ORDER BY {k}").df()
    events = ca.verified_events(cadb.con, "NSE_EQ", 900001)
    frame = ca.adjust_frame(raw, k, events)
    for c in ("open", "high", "low", "close", "volume"):
        assert (abs(frame[c] - view[c]) < 1e-6).all()                                                      # the same rule as the stored view
    assert (frame["cum_price_factor"].iloc[0], frame["cum_price_factor"].iloc[-1]) == (0.5, 1.0)
    # as of the day BEFORE the ex-date the event is not yet known: nothing is adjusted (no look-ahead)
    known_then = ca.verified_events(cadb.con, "NSE_EQ", 900001, as_of=EX - timedelta(days=1))
    assert known_then == [] and (ca.adjust_frame(raw, k, known_then)["close"] == raw["close"]).all()
    on_ex = ca.verified_events(cadb.con, "NSE_EQ", 900001, as_of=EX)
    assert len(on_ex) == 1 and (abs(ca.adjust_frame(raw, k, on_ex)["close"] - view["close"]) < 1e-6).all()


def test_several_events_apply_in_chronological_order_and_as_of_keeps_only_those_already_effective():
    days = pd.date_range("2024-01-01", periods=10, freq="D")
    df = pd.DataFrame({"candle_date": days, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000})
    events = [(date(2024, 1, 4), 0.5, 2.0), (date(2024, 1, 8), 0.2, 5.0)]                      # a 1:1 bonus, later a 5-for-1 split
    out = ca.adjust_frame(df, "candle_date", events)
    assert list(out["cum_price_factor"]) == [0.1, 0.1, 0.1, 0.2, 0.2, 0.2, 0.2, 1.0, 1.0, 1.0]       # before both: x0.1; between: x0.2; after: x1
    assert list(out["volume"][[0, 3, 9]]) == [10000.0, 5000.0, 1000.0]                                # volume scales inversely
    assert out["close"][0] == pytest.approx(10.0) and df["close"][0] == 100.0                          # the input is untouched


def test_candles_from_several_providers_around_an_event_are_never_verified_automatically(cadb):
    seed_events(cadb)
    tf, k = cadb.tf, cadb.tf.key_col
    cadb.con.execute(f"UPDATE {tf.table} SET provider = 'fyers' WHERE security_id = 900001 AND CAST({k} AS DATE) >= DATE '{EX}'")   # post-ex from another source
    ca.evaluate_all(cadb.con, cadb.tf)
    row = cadb.con.execute("SELECT verification_status, apply_adjustment, verification_note FROM corporate_actions WHERE symbol = 'SIMALPHA' AND event_type = 'BONUS'").fetchone()
    assert row[0] == "unresolved" and row[1] is False and "several providers" in row[2]
    assert ca.providers_around(cadb.con, tf, "NSE_EQ", 900001, EX) == ["dhan", "fyers"]
    assert ca.apply_adjustments(cadb.con, tf)["events_applied"] == 0                                    # nothing adjusts on a guess


def test_an_older_database_gains_the_ledger_columns_without_losing_an_event(tmp_path):
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        load_universe(db)
        ca.import_actions(db.con, [rows_for("SIMALPHA", "BONUS", EX, ratio_text="Bonus 1:1", bonus_new=1, bonus_held=1)],
                          source="NSE", source_ref="x", source_confirmed=True)
        for col in ("source_url", "evidence", "retrieved_at_utc", "version", "affected_from", "affected_to", "application_status", "applied_version"):
            db.con.execute(f"ALTER TABLE corporate_actions DROP COLUMN {col}")
    with CandleDB(DAILY, tmp_path / "d.duckdb") as db:
        row = db.con.execute("SELECT symbol, version, application_status, evidence FROM corporate_actions").fetchone()
        assert row == ("SIMALPHA", 1, None, None)
