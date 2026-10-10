"""Corporate actions in the interface: the ledger, verification and adjustment, as explicit audited actions.

The ledger lives inside each candle database (the same two files as always). Reading it uses the normal read-only gate; every
change reserves the database exclusively first (like backup and recovery), so it can never overlap a download job. Only data
the person typed in or pasted from an official record can enter the ledger: nothing is scraped, nothing is inferred from a price
gap, and an unverified event can never trigger an adjustment.
"""
from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import date

import pandas as pd

from stockdata import corp_actions as ca
from stockdata.db import CandleDB
from stockdata.timeframes import TIMEFRAMES, get_timeframe
from stockdata.web.data import DataAccess, DatabaseMissing
from stockdata.web.store import Store
from stockdata.web.supervisor import JobError

MAX_CSV_BYTES = 5 * 1024 * 1024


def _plain(v):
    """A JSON-safe value from a DataFrame cell: missing values (NaN, None, NaT) become None, dates become ISO strings."""
    if v is None or (not isinstance(v, (list, tuple, dict)) and pd.isna(v)):
        return None
    if isinstance(v, (date, pd.Timestamp)):
        return v.isoformat()
    return v.item() if hasattr(v, "item") else v


MANUAL_SOURCES = ("NSE corporate actions", "BSE corporate actions", "company filing", "exchange circular", "manual entry")


class CorporateActions:
    def __init__(self, data: DataAccess, store: Store):
        self.data, self.store = data, store

    # ----------------------------------------------------------------------------------- plumbing
    def timeframes(self) -> list[str]:
        return [tf for tf in TIMEFRAMES if self.data.path(tf).exists()]

    @contextmanager
    def _exclusive(self, tfs: list[str], label: str):
        got: list[str] = []
        try:
            for tf in tfs:
                if not self.data.gate.begin_maintenance(tf, label):
                    raise JobError(f"The {tf} database is in use (a download job owns it, or a data view is still open), so the "
                                   "ledger cannot be changed now. Pause the job first. Nothing was changed.", 409)
                got.append(tf)
            yield
        finally:
            for tf in got:
                self.data.gate.end_maintenance(tf)

    def _targets(self, timeframe: str) -> list[str]:
        have = self.timeframes()
        if not have:
            raise DatabaseMissing("No database exists yet. Load the instrument master first (Instruments page).")
        if timeframe == "both":
            return have
        if timeframe not in have:
            raise DatabaseMissing(f"The {timeframe} database does not exist yet.")
        return [timeframe]

    # ------------------------------------------------------------------------------------ reading
    def listing(self, *, status: str | None = None, symbol: str | None = None) -> dict:
        tfs = self.timeframes()
        if not tfs:
            return {"events": [], "summary": {}, "timeframes": []}
        events: dict[str, dict] = {}
        summary = {}
        for tf in tfs:
            with self.data.connect(tf) as con:
                summary[tf] = ca.adjustment_summary(con)
                for r in con.execute("""SELECT a.action_id, a.symbol, a.security_id, a.event_type, a.announcement_date, a.ex_date, a.record_date,
                        a.ratio_text, a.price_factor, a.volume_factor, a.factor_origin, a.source, a.source_ref, a.source_url, a.source_confirmed,
                        a.verification_status, a.verification_note, a.apply_adjustment, a.application_status, a.applied_version, a.affected_from,
                        a.affected_to, a.version, a.evidence, c.basis, c.detail FROM corporate_actions a LEFT JOIN corporate_action_checks c USING (action_id)
                        ORDER BY a.ex_date DESC, a.symbol""").fetchall():
                    keys = ("action_id", "symbol", "security_id", "event_type", "announcement_date", "ex_date", "record_date", "ratio_text",
                            "price_factor", "volume_factor", "factor_origin", "source", "source_ref", "source_url", "source_confirmed",
                            "verification_status", "verification_note", "apply_adjustment", "application_status", "applied_version",
                            "affected_from", "affected_to", "version", "evidence", "basis", "basis_detail")
                    rec = dict(zip(keys, r))
                    e = events.setdefault(rec["action_id"], {**{k: (v.isoformat() if isinstance(v, date) else v) for k, v in rec.items()
                                                                 if k not in ("verification_status", "apply_adjustment", "application_status",
                                                                              "applied_version", "affected_from", "affected_to", "basis", "basis_detail")},
                                                             "by_timeframe": {}})
                    e["by_timeframe"][tf] = {"status": rec["verification_status"], "applied": bool(rec["apply_adjustment"]),
                                             "application_status": rec["application_status"], "applied_version": rec["applied_version"],
                                             "affected_from": rec["affected_from"].isoformat() if rec["affected_from"] else None,
                                             "affected_to": rec["affected_to"].isoformat() if rec["affected_to"] else None,
                                             "basis": rec["basis"], "basis_detail": rec["basis_detail"]}
        out = list(events.values())
        for e in out:
            statuses = {v["status"] for v in e["by_timeframe"].values()}
            e["needs_review"] = bool(statuses & {"unresolved", "proposed"})
            e["overall_status"] = "verified" if statuses == {"verified"} else sorted(statuses)[0] if len(statuses) == 1 else "mixed"
        if status:
            out = [e for e in out if status in {v["status"] for v in e["by_timeframe"].values()} or status == e["overall_status"]]
        if symbol:
            out = [e for e in out if symbol.upper() in e["symbol"]]
        return {"events": out, "summary": summary, "timeframes": tfs,
                "policy": ["Raw candles are never changed; adjusted candles are a separate view built from verified events only.",
                           "Only splits and bonus issues are price-adjusted. Dividends are recorded, never adjusted.",
                           "An event is verified only when its source is confirmed AND the candles show the raw jump; history already adjusted by "
                           "the provider is never adjusted again.",
                           "Nothing is inferred from a price gap. Unverified events are flagged for review and adjust nothing."]}

    def detail(self, action_id: str) -> dict:
        out: dict = {"action_id": action_id, "by_timeframe": {}}
        for tf in self.timeframes():
            with self.data.connect(tf) as con:
                row = con.execute("SELECT * FROM corporate_actions WHERE action_id = ?", [action_id]).fetchdf()
                if row.empty:
                    continue
                ev = {k: _plain(v) for k, v in row.iloc[0].to_dict().items()}
                out.setdefault("event", ev)
                chk = con.execute("SELECT * FROM corporate_action_checks WHERE action_id = ?", [action_id]).fetchdf()
                val = con.execute("SELECT * FROM adjustment_validations WHERE action_id = ? ORDER BY version DESC LIMIT 1", [action_id]).fetchdf()
                revs = con.execute("SELECT version, changed_at_utc, actor, change, old_value, new_value, note FROM corporate_action_revisions "
                                   "WHERE action_id = ? ORDER BY revision_id", [action_id]).fetchall()
                out["by_timeframe"][tf] = {
                    "check": None if chk.empty else {k: _plain(v) for k, v in chk.iloc[0].to_dict().items()},
                    "validation": None if val.empty else {k: (bool(v) if k == "ok" and not pd.isna(v) else _plain(v)) for k, v in val.iloc[0].to_dict().items()},
                    "revisions": [{"version": r[0], "at": str(r[1]), "actor": r[2], "change": r[3], "old": r[4], "new": r[5], "note": r[6]} for r in revs],
                    "providers_around_ex_date": ca.providers_around(con, get_timeframe(tf), ev["exchange_segment"], int(ev["security_id"]),
                                                                    date.fromisoformat(ev["ex_date"][:10])) if ev.get("security_id") is not None else []}
        if "event" not in out:
            raise KeyError(f"unknown corporate action {action_id}")
        return out

    def summary(self) -> dict:
        out = {}
        for tf in self.timeframes():
            with self.data.connect(tf) as con:
                out[tf] = ca.adjustment_summary(con)
        return out

    # ----------------------------------------------------------------------------------- writing
    def add(self, rows: list[dict], *, source: str, source_ref: str, source_confirmed: bool, timeframe: str = "both") -> dict:
        tfs = self._targets(timeframe)
        out = {}
        with self._exclusive(tfs, "corporate actions"):
            for tf in tfs:
                with CandleDB(get_timeframe(tf), self.data.path(tf)) as db:
                    res = ca.import_actions(db.con, rows, source=source, source_ref=source_ref, source_confirmed=source_confirmed)
                    out[tf] = {"inserted": res.inserted, "already_present": res.existing, "skipped": res.skipped, "by_status": res.by_status}
        self.store.audit("corporate_actions.import", None, {"rows": len(rows), "source": source, "confirmed": source_confirmed, "result": out})
        return out

    def import_csv(self, text: str, *, fmt: str, source: str, source_ref: str, source_confirmed: bool, timeframe: str = "both") -> dict:
        if len(text.encode("utf-8", "ignore")) > MAX_CSV_BYTES:
            raise JobError("That file is too large for an import (5 MB maximum).", 400)
        try:
            df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
            rows = ca.rows_from_nse_csv(df) if fmt == "nse" else df.to_dict("records")
        except (ValueError, pd.errors.ParserError) as exc:
            raise JobError(f"The file could not be read as a {'NSE export' if fmt == 'nse' else 'reference-format'} CSV: {exc}", 400)
        if not rows:
            raise JobError("The file has no rows.", 400)
        return self.add(rows, source=source, source_ref=source_ref, source_confirmed=source_confirmed, timeframe=timeframe)

    def evaluate(self, timeframe: str = "both", reference_csv: str | None = None) -> dict:
        reference: dict = {}
        if reference_csv:
            try:
                r = pd.read_csv(io.StringIO(reference_csv), dtype=str, keep_default_na=False)
                for _, x in r.iterrows():
                    reference[(x["symbol"].strip().upper(), ca._date(x["date"]))] = float(x["close"])
            except (KeyError, ValueError) as exc:
                raise JobError(f"The reference-price file needs the columns symbol,date,close: {exc}", 400)
        tfs = self._targets(timeframe)
        out = {}
        with self._exclusive(tfs, "corporate-action verification"):
            for tf in tfs:
                with CandleDB(get_timeframe(tf), self.data.path(tf)) as db:
                    df = ca.evaluate_all(db.con, db.tf, reference=reference)
                    out[tf] = [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in rec.items()} for rec in df.to_dict("records")]
        self.store.audit("corporate_actions.evaluate", None, {"timeframes": tfs, "events": {tf: len(v) for tf, v in out.items()}})
        return out

    def verify(self, action_id: str, status: str, note: str) -> dict:
        if status not in ca.STATUSES:
            raise JobError(f"status must be one of {', '.join(ca.STATUSES)}", 400)
        if not note.strip():
            raise JobError("A note is required: who verified what, against which official source.", 400)
        done = []
        tfs = self.timeframes()
        with self._exclusive(tfs, "corporate-action decision"):
            for tf in tfs:
                with CandleDB(get_timeframe(tf), self.data.path(tf)) as db:
                    if db.con.execute("SELECT 1 FROM corporate_actions WHERE action_id = ?", [action_id]).fetchone():
                        ca.set_manual_status(db.con, action_id, status, note)
                        done.append(tf)
        if not done:
            raise KeyError(f"unknown corporate action {action_id}")
        self.store.audit("corporate_actions.verify", f"action:{action_id}", {"status": status, "timeframes": done})
        return {"action_id": action_id, "status": status, "timeframes": done}

    def apply(self, timeframe: str = "both") -> dict:
        tfs = self._targets(timeframe)
        out = {}
        with self._exclusive(tfs, "corporate-action adjustment"):
            for tf in tfs:
                with CandleDB(get_timeframe(tf), self.data.path(tf)) as db:
                    res = ca.apply_adjustments(db.con, db.tf)
                    out[tf] = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in res.items() if k != "validations"}
                    out[tf]["validations"] = [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in rec.items()} for rec in res["validations"]]
        self.store.audit("corporate_actions.apply", None, {tf: {"version": r["version"], "events": r["events_applied"], "failures": r["validation_failures"]}
                                                           for tf, r in out.items()})
        return out

    def scan(self, timeframe: str) -> dict:
        tfs = self._targets(timeframe)
        out = {}
        with self._exclusive(tfs, "gap scan"):
            for tf in tfs:
                with CandleDB(get_timeframe(tf), self.data.path(tf)) as db:
                    df = ca.scan_gaps(db.con, db.tf)
                    out[tf] = [{k: _plain(v) for k, v in rec.items()}
                               for rec in df.head(500).to_dict("records")]
        return {"findings": out, "note": "These are overnight gaps that LOOK like unrecorded splits or bonuses. They are leads to check against an "
                                         "official record, never events: nothing here adjusts anything."}
