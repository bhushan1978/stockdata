"""Provider management for the interface: capabilities with their verification status, credentials (never values),
connection tests, capability probes, default-provider choice and per-provider request budgets.

Everything that contacts a provider is an explicit action that spends a small, counted number of requests through the same
shared ledger as every download; nothing here runs on its own.
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone

from stockdata.budget import RequestBudget, UsageLedger
from stockdata.providers import PROVIDER_NAMES, get_provider, ledger_label
from stockdata.providers.base import PROBE_VERIFIED
from stockdata.providers.probe import run_probe
from stockdata.timeframes import TIMEFRAMES
from stockdata.timeutil import IST
from stockdata.web.redact import redact
from stockdata.web.supervisor import JobError

BASE_URL_KEYS = {"dhan": "DHAN_BASE_URL", "fyers": "FYERS_BASE_URL", "upstox": "UPSTOX_BASE_URL"}
TEST_BUDGET = {"per_run": 5, "label": "test"}
PROBE_BUDGET = {"per_run": 40, "label": "probe"}


class ProviderService:
    def __init__(self, settings, store, creds, sup, data, gate):
        self.s, self.store, self.creds, self.sup, self.data, self.gate = settings, store, creds, sup, data, gate

    # ---------------------------------------------------------------------------------- plumbing
    def _env(self, provider: str) -> dict[str, str]:
        env = dict(self.creds.provider_env(provider))
        override = os.environ.get(BASE_URL_KEYS[provider])
        if override:
            env[BASE_URL_KEYS[provider]] = override
        return env

    def _client(self, provider: str, purpose: dict):
        adapter = get_provider(provider)
        env = self._env(provider)
        problem = self.sup.provider_ready(provider)
        if problem and "not configured" in problem:
            raise JobError(problem, 400)
        lim = self.sup.provider_limits(provider)
        ledger = UsageLedger(self.s.engine.request_ledger_path)
        budget = RequestBudget(ledger, ledger_label(provider, purpose["label"]), max_per_run=purpose["per_run"],
                               max_per_day=lim["per_day"], provider=provider)
        engine = self.s.engine
        if provider == "dhan":                                  # credentials may have changed since the server started
            engine = dataclasses.replace(engine, access_token=env.get("DHAN_ACCESS_TOKEN"), client_id=env.get("DHAN_CLIENT_ID"))
        try:
            return adapter.make_client(env, settings=engine, label=purpose["label"], budget=budget), ledger
        except Exception:
            ledger.close()
            raise

    def _record(self, provider: str, kind: str, ok: bool, state: str, message: str, details: dict) -> dict:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.execute("INSERT INTO provider_checks (provider, kind, ts, ok, state, message, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (provider, kind, ts, int(ok), state, redact(message)[:500], json.dumps(details)))
        return {"provider": provider, "kind": kind, "ts": ts, "ok": ok, "state": state, "message": redact(message)[:500], "details": details}

    def checks(self, provider: str, kind: str | None = None, limit: int = 10) -> list[dict]:
        q, args = "SELECT * FROM provider_checks WHERE provider = ?", [provider]
        if kind:
            q, args = q + " AND kind = ?", args + [kind]
        rows = self.store.all(q + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [{"id": r["id"], "kind": r["kind"], "ts": r["ts"], "ok": bool(r["ok"]), "state": r["state"], "message": r["message"],
                 "details": json.loads(r["details"] or "{}")} for r in rows]

    # ----------------------------------------------------------------------------------- actions
    def test_connection(self, provider: str) -> dict:
        """One counted request that proves the stored credential works (no candles). Never stores or returns a secret."""
        adapter = get_provider(provider)
        client, ledger = self._client(provider, TEST_BUDGET)
        try:
            res = adapter.test_connection(client)
        finally:
            ledger.close()
        self.store.audit("provider.test", f"provider:{provider}", {"ok": res.ok, "state": res.state})
        return self._record(provider, "connection", res.ok, res.state, res.message,
                            {"requests_used": res.requests_used, **res.details})

    def probe(self, provider: str, symbol: str | None = None) -> dict:
        """The capability probe (see stockdata/providers/probe.py): about a dozen counted requests for ONE instrument."""
        from stockdata.db import CandleDB
        from stockdata.timeframes import DAILY
        adapter = get_provider(provider)
        path = self.data.path("daily")
        if not path.exists():
            raise JobError("Load the instrument master first (Instruments page): the probe needs one instrument to ask about.", 400)
        wanted = (symbol or "RELIANCE").strip().upper()
        with self.gate.reader("daily"):
            with CandleDB(DAILY, path, read_only=True) as db:
                found = db.get_instruments(symbols=[wanted]) or db.get_instruments()[:1]
        if not found:
            raise JobError("No instrument is loaded to probe with.", 400)
        inst = found[0]
        if adapter.map_instrument(inst) is None:
            raise JobError(f"{adapter.label} has no identifier for {inst.symbol}"
                           + (" (load the DETAILED instrument master to get ISINs)" if provider == "upstox" else ""), 400)
        client, ledger = self._client(provider, PROBE_BUDGET)
        try:
            report = run_probe(adapter, client, inst)
        finally:
            ledger.close()
        self.store.audit("provider.probe", f"provider:{provider}", {"state": report.state, "requests": report.requests_used})
        return self._record(provider, "probe", report.state in ("ok", "partial"), report.state, report.message or "capability probe finished",
                            report.to_dict())

    # ---------------------------------------------------------------------------------- reading
    def capabilities(self, provider: str) -> dict:
        """Published facts, upgraded with whatever the latest probe MEASURED (and clearly labelled which is which)."""
        caps = get_provider(provider).capabilities().to_dict()
        probes = self.checks(provider, "probe", 1)
        probe = probes[0] if probes else None
        for tf_name, tf in caps["timeframes"].items():
            ver = ((probe or {}).get("details", {}).get("verified") or {}).get(tf_name, {})
            used = get_provider(provider).window_days(TIMEFRAMES[tf_name])
            tf["verified"] = {"probed_at": probe["ts"] if probe else None,
                              "max_window_days_accepted": ver.get("max_window_days_accepted"),
                              "has_data_since": ver.get("has_data_since"),
                              "data_before_documented_first": ver.get("data_before_documented_first", False)}
            tf["max_window_status"] = (PROBE_VERIFIED if ver.get("max_window_days_accepted") and ver["max_window_days_accepted"] >= used
                                       else tf["status"])
            tf["coverage_status"] = PROBE_VERIFIED if ver.get("has_data_since") else tf["status"]
        return caps

    def unattended(self, provider: str) -> dict:
        caps = get_provider(provider).capabilities()
        ran = [r for r in self.store.all("SELECT summary, status FROM automation_runs WHERE trigger = 'scheduled' AND status IN "
                                         "('completed', 'completed_with_warnings') ORDER BY id DESC LIMIT 20")
               if provider in (json.loads(r["summary"] or "{}").get("providers") or [])]
        if caps.auth.interactive_login:
            status = "requires_login"
        else:
            status = "verified" if ran else "unverified"
        return {"status": status, "note": caps.auth.unattended_note,
                "verified_by_scheduled_run": bool(ran), "interactive_login_required": caps.auth.interactive_login}

    def overview(self) -> dict:
        eff = self.sup.effective()
        usage = self.sup.usage()
        providers = []
        for name in PROVIDER_NAMES:
            adapter = get_provider(name)
            status = self.creds.provider_status(name)
            conn = next(iter(self.checks(name, "connection", 1)), None)
            probe = next(iter(self.checks(name, "probe", 1)), None)
            providers.append({
                "provider": name, "label": adapter.label, "is_default": eff["provider_default"] == name,
                "default_for": [tf for tf, p in eff["provider_by_timeframe"].items() if p == name],
                "capabilities": self.capabilities(name), "credentials": status, "auth": status["auth"],
                "unattended": self.unattended(name),
                "usage": usage["providers"][name], "limits": self.sup.provider_limits(name),
                "last_connection": conn, "last_probe": ({k: probe[k] for k in ("ts", "ok", "state", "message")} if probe else None)})
        return {"default": eff["provider_default"], "by_timeframe": eff["provider_by_timeframe"], "live_reserve": eff["live_reserve"],
                "providers": providers,
                "policy": {"source_precedence": ["the provider of a job is fixed when the job is created",
                                                 "existing candles are never replaced because the default changed",
                                                 "a re-fetched candle that differs is recorded as a disagreement and the stored one is kept",
                                                 "replacing candles from another provider is a separate, explicit, audited repair"],
                           "same_databases": "every provider writes into the same two databases and the same candle format"}}

    # ---------------------------------------------------------------------------------- settings
    def set_defaults(self, *, default: str | None = None, by_timeframe: dict | None = None, live_reserve: int | None = None) -> dict:
        changes: dict = {}
        if default is not None:
            changes["provider_default"] = default
        if by_timeframe is not None:
            merged = dict(self.sup.effective()["provider_by_timeframe"])
            for tf, prov in by_timeframe.items():
                if prov in (None, ""):
                    merged.pop(tf, None)
                else:
                    merged[tf] = prov
            changes["provider_by_timeframe"] = merged
        if live_reserve is not None:
            changes["live_reserve"] = live_reserve
        self.sup.update_settings(**changes)
        return {**self.overview(), "unchanged": ["no database was created or modified", "no checkpoint was reset",
                                                  "no stored candle was replaced", "queued jobs keep the provider they were created with"]}

    def set_limits(self, provider: str, *, per_day: int | None, per_run: int | None) -> dict:
        saved = dict(self.sup.effective()["provider_limits"])
        cur = dict(saved.get(provider) or {})
        if per_day is not None:
            cur["per_day"] = per_day
        if per_run is not None:
            cur["per_run"] = per_run
        saved[provider] = cur
        self.sup.update_settings(provider_limits=saved)
        return {"provider": provider, "limits": self.sup.provider_limits(provider)}
