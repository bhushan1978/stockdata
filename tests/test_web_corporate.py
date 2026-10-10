"""Corporate actions and adjusted history through the web API: the ledger, verification, versioned application, labelled raw and
adjusted candles (table, chart data and CSV), the as-of date for look-ahead-free backtests, and exclusive access while a download
owns a database. Synthetic series only."""
import calendar
import csv
import io
from datetime import date, timedelta

import pytest

from stockdata import corp_actions as ca
from stockdata.db import CandleDB
from stockdata.timeframes import DAILY, INTRADAY_15
from test_corp_actions import EX, bars, put
from web_helpers import Env


@pytest.fixture
def cenv(tmp_path):
    env = Env(tmp_path)
    for tf in (DAILY, INTRADAY_15):
        with CandleDB(tf, env.settings.data_dir / tf.db_name) as db:
            put(db, 900001, bars(0.5, "raw", EX, seed=1))                              # SIMALPHA: raw history, halves at the ex-date
            put(db, 900002, bars(0.2, "adjusted", date(2024, 4, 15), base=800.0, seed=2))      # SIMBETA: history already adjusted
    yield env
    env.close()


BONUS = {"symbol": "SIMALPHA", "event_type": "BONUS", "ex_date": EX.isoformat(), "ratio_text": "Bonus 1:1", "bonus_new": 1, "bonus_held": 1,
         "source": "NSE corporate actions", "source_ref": "NSE circular 2024/0123", "source_confirmed": True, "confirm": True,
         "evidence": "Corporate Filings > Corporate Actions, retrieved 2024-03-01", "announcement_date": "2024-02-20"}


def add(env, **kw):
    r = env.client.post("/api/corporate-actions", json={**BONUS, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def listing(env, **params):
    return env.client.get("/api/corporate-actions", params=params).json()


def go(env, path, body=None, expect=200):
    r = env.client.post(path, json={"confirm": True, **(body or {})})
    assert r.status_code == expect, r.text
    return r.json()


def alpha_id(env):
    return next(e["action_id"] for e in listing(env)["events"] if e["symbol"] == "SIMALPHA" and e["event_type"] == "BONUS")


# ------------------------------------------------------------------------------------------------ the flow
def test_a_verified_split_or_bonus_goes_from_the_ledger_to_a_labelled_adjusted_series_and_raw_stays_raw(cenv):
    assert cenv.client.post("/api/corporate-actions", json={**BONUS, "confirm": False}).status_code == 400          # an explicit action
    out = add(cenv)
    assert out["daily"]["inserted"] == 1 and out["15min"]["inserted"] == 1                                         # the same event in both databases
    ev = listing(cenv)["events"][0]
    assert ev["by_timeframe"]["daily"]["status"] == "proposed" and ev["needs_review"] and ev["source_url"] is None
    assert ev["evidence"].startswith("Corporate Filings") and ev["announcement_date"] == "2024-02-20"
    ev_out = go(cenv, "/api/corporate-actions/evaluate", {"timeframe": "both"})
    assert {r["status"] for tf in ev_out.values() for r in tf} == {"verified"}
    ap = go(cenv, "/api/corporate-actions/apply", {"timeframe": "both"})
    assert ap["daily"]["version"] == 1 and ap["15min"]["version"] == 1 and ap["daily"]["validation_failures"] == 0
    ev = listing(cenv)["events"][0]
    d = ev["by_timeframe"]["daily"]
    assert d["application_status"] == "applied" and d["applied_version"] == 1 and d["affected_to"] == (EX - timedelta(days=1)).isoformat()
    for tf in ("daily", "15min"):
        raw = cenv.client.get("/api/candles", params={"timeframe": tf, "security_id": 900001, "bars": 5000}).json()
        adj = cenv.client.get("/api/candles", params={"timeframe": tf, "security_id": 900001, "bars": 5000, "basis": "adjusted"}).json()
        assert raw["basis"] == "raw" and raw["adjustment"]["label"].startswith("RAW") and raw["adjustment"]["status"] == "raw"
        assert adj["basis"] == "adjusted" and adj["adjustment"]["label"].startswith("SPLIT/BONUS ADJUSTED") and adj["adjustment"]["version"] == 1
        assert adj["adjustment"]["status"] == "adjusted" and adj["adjustment"]["events_applied"][0]["price_factor"] == 0.5
        ex_epoch = calendar.timegm(EX.timetuple())                                        # midnight of the ex-date as an IST wall clock
        pre = [i for i, t in enumerate(raw["t"]) if t < ex_epoch]
        post = [i for i, t in enumerate(raw["t"]) if t >= ex_epoch]
        assert pre and post
        assert all(abs(adj["c"][i] - raw["c"][i] * 0.5) < 1e-6 and abs(adj["v"][i] - raw["v"][i] * 2.0) < 1e-6 for i in pre)     # price x0.5, volume x2, before the ex-date
        assert all(adj["c"][i] == raw["c"][i] and adj["v"][i] == raw["v"][i] for i in post)                                          # the ex-date and after: untouched
        assert [m["type"] for m in raw["corporate_actions"]] == ["BONUS"] and raw["corporate_actions"][0]["adjusts"] is True       # the chart can mark it
        assert raw["c"] != adj["c"] and raw["n"] == adj["n"]                                                                          # no bar added or lost


def test_as_of_gives_the_series_as_it_was_known_then_without_look_ahead(cenv):
    add(cenv)
    go(cenv, "/api/corporate-actions/evaluate")
    go(cenv, "/api/corporate-actions/apply")
    p = {"timeframe": "daily", "security_id": 900001, "bars": 5000, "basis": "adjusted"}
    raw = cenv.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001, "bars": 5000}).json()
    before = cenv.client.get("/api/candles", params={**p, "as_of": (EX - timedelta(days=1)).isoformat()}).json()
    assert before["adjustment"]["status"] == "no_verified_events" and before["c"] == raw["c"] and "as known on" in before["adjustment"]["label"]
    assert "NO verified event applies" in before["adjustment"]["label"]
    on = cenv.client.get("/api/candles", params={**p, "as_of": EX.isoformat()}).json()
    full = cenv.client.get("/api/candles", params=p).json()
    assert on["c"] == full["c"] and on["adjustment"]["as_of"] == EX.isoformat()
    assert cenv.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001, "as_of": EX.isoformat()}).status_code == 400   # as_of needs the adjusted basis


def test_the_table_and_the_csv_export_carry_the_basis_provider_and_revision_information(cenv):
    add(cenv)
    go(cenv, "/api/corporate-actions/evaluate")
    go(cenv, "/api/corporate-actions/apply")
    base = {"timeframe": "daily", "security_id": 900001, "sort": "time", "desc": False, "page_size": 3}
    raw = cenv.client.get("/api/candles/table", params=base).json()
    adj = cenv.client.get("/api/candles/table", params={**base, "basis": "adjusted"}).json()
    assert raw["label"].startswith("RAW") and adj["label"].startswith("SPLIT/BONUS ADJUSTED")
    r0, a0 = raw["rows"][0], adj["rows"][0]
    assert r0["provider"] == "dhan" and r0["revisions"] == 0 and r0["basis"] == "raw" and a0["basis"] == "adjusted"      # NULL provider = Dhan (legacy)
    assert a0["close"] == pytest.approx(r0["close"] * 0.5) and a0["volume"] == pytest.approx(r0["volume"] * 2)
    assert cenv.client.get("/api/candles/table", params={**base, "basis": "adjusted", "sort": "close"}).status_code == 400  # not derivable per row
    rows = list(csv.DictReader(io.StringIO(cenv.client.get("/api/candles/export.csv", params={"timeframe": "daily", "security_id": 900001}).text)))
    arows = list(csv.DictReader(io.StringIO(cenv.client.get("/api/candles/export.csv", params={"timeframe": "daily", "security_id": 900001, "basis": "adjusted"}).text)))
    assert {r["basis"] for r in rows} == {"raw"} and {r["basis"] for r in arows} == {"adjusted_v1"} and {r["provider"] for r in rows} == {"dhan"}
    assert float(arows[0]["close"]) == pytest.approx(float(rows[0]["close"]) * 0.5) and float(arows[-1]["close"]) == pytest.approx(float(rows[-1]["close"]))
    resp = cenv.client.get("/api/candles/export.csv", params={"timeframe": "daily", "security_id": 900001, "basis": "adjusted", "as_of": EX.isoformat()})
    assert "adjusted_as_of" in resp.headers["content-disposition"] and "adjusted_v1_as_of_" in resp.text


# ----------------------------------------------------------------------------------------------- the rules
def test_an_event_whose_source_is_not_confirmed_never_adjusts_anything(cenv):
    add(cenv, source_confirmed=False)
    go(cenv, "/api/corporate-actions/evaluate")
    ev = listing(cenv)["events"][0]
    assert ev["by_timeframe"]["daily"]["status"] == "proposed" and ev["by_timeframe"]["daily"]["applied"] is False and ev["needs_review"]
    ap = go(cenv, "/api/corporate-actions/apply")
    assert ap["daily"]["events_applied"] == 0
    raw = cenv.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001, "bars": 5000}).json()
    adj = cenv.client.get("/api/candles", params={"timeframe": "daily", "security_id": 900001, "bars": 5000, "basis": "adjusted"}).json()
    assert adj["c"] == raw["c"] and adj["adjustment"]["status"] == "no_verified_events"


def test_history_the_provider_already_adjusted_is_never_adjusted_a_second_time(cenv):
    add(cenv, symbol="SIMBETA", event_type="SPLIT", ex_date="2024-04-15", ratio_text="Split 10 to 2", old_face_value=10, new_face_value=2,
        bonus_new=None, bonus_held=None, evidence="NSE circular")
    go(cenv, "/api/corporate-actions/evaluate")
    ev = next(e for e in listing(cenv)["events"] if e["symbol"] == "SIMBETA")
    assert ev["by_timeframe"]["daily"]["status"] == "source_adjusted" and "NOT applied again" in ev["verification_note"]
    assert go(cenv, "/api/corporate-actions/apply")["daily"]["events_applied"] == 0


def test_dividends_are_recorded_but_never_become_adjustments(cenv):
    add(cenv, event_type="DIVIDEND", ratio_text="Dividend Rs 5", cash_amount=5, bonus_new=None, bonus_held=None, ex_date="2024-04-01")
    go(cenv, "/api/corporate-actions/evaluate")
    ev = listing(cenv)["events"][0]
    assert ev["by_timeframe"]["daily"]["status"] == "recorded_not_adjusted" and ev["price_factor"] is None
    assert go(cenv, "/api/corporate-actions/apply")["daily"]["events_applied"] == 0


def test_a_decision_needs_a_note_a_confirmation_and_leaves_a_revision_trail(cenv):
    add(cenv, event_type="RIGHTS", ratio_text="Rights 1:4 @ 80", rights_new=1, rights_held=4, issue_price=80, bonus_new=None, bonus_held=None,
        ex_date="2024-04-22")
    go(cenv, "/api/corporate-actions/evaluate")
    aid = next(e["action_id"] for e in listing(cenv)["events"] if e["event_type"] == "RIGHTS")
    assert listing(cenv)["events"][0]["by_timeframe"]["daily"]["status"] == "unresolved"
    assert cenv.client.post(f"/api/corporate-actions/{aid}/verify", json={"status": "verified", "note": "NSE circular x", "confirm": False}).status_code == 400
    assert cenv.client.post(f"/api/corporate-actions/{aid}/verify", json={"status": "verified", "note": "", "confirm": True}).status_code == 422
    ok = go(cenv, f"/api/corporate-actions/{aid}/verify", {"status": "verified", "note": "NSE circular NSE/CML/2024/10: TERP factor confirmed"})
    assert ok["timeframes"] == ["daily", "15min"]
    detail = cenv.client.get(f"/api/corporate-actions/{aid}").json()
    revs = detail["by_timeframe"]["daily"]["revisions"]
    assert [r["change"] for r in revs][0] == "created" and revs[-1]["change"] == "manual_decision" and revs[-1]["actor"] == "person"
    assert "NSE/CML/2024/10" in revs[-1]["note"] and detail["event"]["version"] >= 2


def test_candles_from_two_providers_around_the_ex_date_are_flagged_for_a_person(cenv):
    with CandleDB(DAILY, cenv.settings.data_dir / DAILY.db_name) as db:
        db.con.execute(f"UPDATE candles_daily SET provider = 'fyers' WHERE security_id = 900001 AND candle_date >= DATE '{EX}'")
    add(cenv)
    go(cenv, "/api/corporate-actions/evaluate", {"timeframe": "daily"})
    aid = alpha_id(cenv)
    detail = cenv.client.get(f"/api/corporate-actions/{aid}").json()
    assert detail["by_timeframe"]["daily"]["providers_around_ex_date"] == ["dhan", "fyers"]
    ev = next(e for e in listing(cenv)["events"] if e["action_id"] == aid)
    assert ev["by_timeframe"]["daily"]["status"] == "unresolved" and "several providers" in ev["verification_note"]


def test_the_ledger_cannot_be_changed_while_a_download_owns_a_database_and_nothing_is_half_done(cenv):
    cenv.store.execute("INSERT INTO jobs (name, kind, timeframe, status, options, queue_pos, created_at) VALUES ('writer', 'download', '15min', 'running', ?, 1, 'x')",
                       ('{"spec": {"kind": "download", "timeframe": "15min", "selection": {"mode": "all"}, "confirm_full": true}}',))
    r = cenv.client.post("/api/corporate-actions", json=BONUS)
    assert r.status_code == 409 and "Nothing was changed" in r.json()["error"] and "15min" in r.json()["error"]
    with CandleDB(DAILY, cenv.settings.data_dir / DAILY.db_name, read_only=True) as db:
        assert db.con.execute("SELECT count(*) FROM corporate_actions").fetchone()[0] == 0         # the daily database was not changed either
    assert cenv.client.post("/api/corporate-actions/apply", json={"confirm": True}).status_code == 409


# ------------------------------------------------------------------------------------------------ importing
def test_a_reference_csv_can_be_imported_and_an_unreadable_file_is_refused(cenv):
    text = ("symbol,event_type,ex_date,ratio_text,bonus_new,bonus_held,source_ref\n"
            f"SIMALPHA,BONUS,{EX.isoformat()},Bonus 1:1,1,1,NSE circular 2024/0123\n")
    r = cenv.client.post("/api/corporate-actions/import", json={"text": text, "format": "reference", "source_ref": "NSE export 2024-03-01",
                                                                 "source_confirmed": True, "confirm": True})
    assert r.status_code == 200 and r.json()["daily"]["inserted"] == 1
    again = cenv.client.post("/api/corporate-actions/import", json={"text": text, "format": "reference", "source_ref": "NSE export 2024-03-01",
                                                                    "source_confirmed": True, "confirm": True}).json()
    assert again["daily"]["inserted"] == 0 and again["daily"]["already_present"] == 1             # re-importing never resets a decision
    bad = cenv.client.post("/api/corporate-actions/import", json={"text": "not,a,real\n", "format": "nse", "source_ref": "x-ref", "confirm": True})
    assert bad.status_code == 400 and "could not be read" in bad.json()["error"]
    assert cenv.client.post("/api/corporate-actions/import", json={"text": text, "format": "reference", "source_ref": "x-ref"}).status_code == 400


def test_scanning_for_gaps_produces_leads_never_events(cenv):
    out = go(cenv, "/api/corporate-actions/scan", {"timeframe": "daily"})
    gaps = out["findings"]["daily"]
    assert any(g["security_id"] == 900001 and g["nearest_ratio"] and g["matched_action_id"] is None for g in gaps)      # the unrecorded halving is found ...
    assert "never events" in out["note"] and listing(cenv)["events"] == []                                             # ... but nothing was created
    assert go(cenv, "/api/corporate-actions/apply")["daily"]["events_applied"] == 0
