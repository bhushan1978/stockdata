"""Job queue, scheduler and worker supervision.

The supervisor is the single authority for who may write a database:

* a database has at most ONE active worker (checked here, enforced by the data gate, and finally by DuckDB's own
  file lock), and all workers share the one request ledger through the engine's normal request budget;
* workers are detached processes running the existing CLI, so they survive a server restart. On start the
  supervisor re-adopts a live worker, recovers the result of one that finished while the server was down, or marks
  the job ``interrupted`` - it never starts a second worker for a job that may still be running;
* pause/stop are *graceful*: a stop-request file is honoured by the engine between windows (the in-flight API call
  finishes and is committed). Force-stop is a separate, explicit hard kill.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stockdata import jobcontrol
from stockdata.budget import DHAN_DOCUMENTED_DAILY_LIMIT, counter_caveat, read_usage, validate_limits
from stockdata.providers import PROVIDER_NAMES, get_provider
from stockdata import live
from stockdata.db import CandleDB
from stockdata.downloader import select_instruments
from stockdata.plan import PlanError, build_download_plan
from stockdata.refresh import Refresher, RefreshOptions
from stockdata.timeframes import get_timeframe
from stockdata.timeutil import IST, cutoff_exclusive, to_midnight
from stockdata.web import jobs as J
from stockdata.web.credentials import CredentialStore, all_provider_keys
from stockdata.web.data import DataAccess, DatabaseMissing, DbGate
from stockdata.web.process import free_bytes, hard_kill, is_alive, spawn_detached
import psutil
from stockdata.web.redact import redact
from stockdata.web.settings import WebSettings
from stockdata.web.store import ACTIVE, FINISHED_OK, RESUMABLE, TERMINAL, Store, now

log = logging.getLogger("stockdata.web.supervisor")

TICK_SECONDS = 2.0
STALE_AFTER = 120            # seconds without a heartbeat from a live worker
MIN_FREE_BYTES = 2 * 1024 ** 3
PARALLEL_RPS = 2.0           # each worker's rate when daily and 15-minute run side by side (2 + 2 < Dhan's 5/s)
LIVE_RPS = 1.5               # a live update runs beside a historical download (3 + 1.5 < Dhan's 5/s); other providers: 20% of their rate
LIVE_RATE_FRACTION = 0.2
DEFAULT_LIVE_RESERVE = 2_000 # requests per day a historical job leaves untouched so live updates can always run
DEFAULT_BYTES_PER_ROW = 80   # synthetic benchmark: 77 bytes/row at 14.4 M rows (no real-data evidence yet)
DAILY_ROWS_UPPER = 6_300     # largest real daily history seen: 6,159 candles (RELIANCE, 2002 -> 2026)
INTRADAY_ROWS_PER_WINDOW = 1_650   # 89 days x ~26 bars; real 89-day windows returned about 1,527


class JobError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _dt(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


class Supervisor:
    def __init__(self, settings: WebSettings, store: Store, creds: CredentialStore, data: DataAccess, gate: DbGate):
        self.s, self.store, self.creds, self.data, self.gate = settings, store, creds, data, gate
        self._lock = threading.RLock()
        self._procs: dict[int, object] = {}            # run id -> Popen (children only; for reaping/exit codes)
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stale_noted: set[int] = set()

    # ------------------------------------------------------------------ effective settings
    def effective(self) -> dict:
        eng = self.s.engine
        g = self.store.get_meta
        return {
            "rps": g("rps", eng.rps), "max_per_run": g("max_per_run", eng.max_requests_per_run),
            "max_per_day": g("max_per_day", eng.max_requests_per_day),
            "parallel_timeframes": g("parallel_timeframes", False), "queue_enabled": g("queue_enabled", True),
            "auto_resume_budget": g("auto_resume_budget", False), "min_free_gb": g("min_free_gb", 5),
            "provider_default": g("provider_default", eng.provider or "dhan"),
            "provider_by_timeframe": g("provider_by_timeframe", {}) or {},
            "live_reserve": g("live_reserve", DEFAULT_LIVE_RESERVE),
            "provider_limits": g("provider_limits", {}) or {},
        }

    # ------------------------------------------------------------------ providers
    def default_provider(self, timeframe: str | None = None) -> str:
        eff = self.effective()
        return eff["provider_by_timeframe"].get(timeframe) or eff["provider_default"]

    def provider_limits(self, provider: str) -> dict:
        """Per-run and per-IST-day request caps for one provider. Each provider has its own account-wide limits."""
        eff = self.effective()
        saved = eff["provider_limits"].get(provider) or {}
        cap = get_provider(provider).capabilities().rate_limit
        if provider == "dhan":
            per_day, per_run = saved.get("per_day", eff["max_per_day"]), saved.get("per_run", eff["max_per_run"])
        else:
            per_day = saved.get("per_day") or cap.operating_day_cap or eff["max_per_day"]
            per_run = saved.get("per_run") or min(eff["max_per_run"], per_day)
        return {"per_run": int(per_run), "per_day": int(per_day)}

    def provider_ready(self, provider: str) -> str | None:
        """None when the provider's credentials look usable, otherwise why not (offline: nothing is contacted)."""
        if provider == "dhan":                                   # the long-standing wording, kept exactly
            st = self.creds.status()
            if not st["ready"]:
                return "Dhan credentials are not configured (Settings -> Credentials)"
            if st["access_token"].get("expired"):
                return "the Dhan access token has expired (Settings -> Credentials)"
            return None
        auth = self.creds.provider_status(provider)["auth"]
        label = get_provider(provider).label
        if auth["state"] == "not_configured":
            return f"{label} credentials are not configured (Providers screen)"
        if auth["state"] == "expired":
            return f"the {label} token has expired (Providers screen): ACTION REQUIRED"
        return None

    def update_settings(self, **changes) -> dict:
        """Change settings. Only the values being changed are validated, so switching the queue on or off keeps working
        even if an unrelated value that comes from the environment (for example STOCKDATA_RPS) is outside the allowed range."""
        changed = {k: v for k, v in changes.items() if v is not None}
        cur = self.effective()
        cur.update(changed)
        if "max_per_run" in changed or "max_per_day" in changed:
            try:
                validate_limits(int(cur["max_per_run"]), int(cur["max_per_day"]))
            except ValueError as exc:
                raise JobError(str(exc), 400)
        if "rps" in changed and not 0 < float(cur["rps"]) <= 5:
            raise JobError("requests per second must be above 0 and at most Dhan's documented 5", 400)
        if "provider_default" in changed and changed["provider_default"] not in PROVIDER_NAMES:
            raise JobError(f"unknown provider {changed['provider_default']!r}", 400)
        if "provider_by_timeframe" in changed:
            from stockdata.timeframes import TIMEFRAMES
            for tf_name, prov in changed["provider_by_timeframe"].items():
                if tf_name not in TIMEFRAMES or prov not in PROVIDER_NAMES:
                    raise JobError(f"invalid provider choice for {tf_name!r}: {prov!r}", 400)
                if not get_provider(prov).supports(TIMEFRAMES[tf_name]):
                    raise JobError(f"{get_provider(prov).label} does not provide {tf_name} candles", 400)
        if "live_reserve" in changed and int(changed["live_reserve"]) < 0:
            raise JobError("the live reserve cannot be negative", 400)
        if "provider_limits" in changed:
            for prov, lim in changed["provider_limits"].items():
                if prov not in PROVIDER_NAMES:
                    raise JobError(f"unknown provider {prov!r}", 400)
                documented = get_provider(prov).capabilities().rate_limit.per_day
                for key in ("per_day", "per_run"):
                    if key in lim and int(lim[key]) < 1:
                        raise JobError("request limits must be positive integers", 400)
                if documented and int(lim.get("per_day", 0)) > documented:
                    raise JobError(f"the per-day limit {int(lim['per_day']):,} exceeds {get_provider(prov).label}'s documented "
                                   f"{documented:,} requests/day; refusing to configure it", 400)
        for k, v in changed.items():
            self.store.set_meta(k, v)
        self.store.audit("settings.update", None, changed)
        self.wake()
        return self.effective()

    # ------------------------------------------------------------------ budget
    def usage(self, provider: str | None = None) -> dict:
        """The request budget of ONE provider (the default provider unless told otherwise) plus a short summary of all."""
        eff = self.effective()
        provider = provider or self.default_provider("daily")
        lim = self.provider_limits(provider)
        ledger = self.s.engine.request_ledger_path
        today = read_usage(ledger, provider=provider)
        used = sum(today.values())
        now_ist = datetime.now(IST)
        reset = (now_ist + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        events = []
        if ledger.exists():
            import sqlite3
            try:
                con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=10)
                events = [{"ts_utc": r[0], "day": r[1], "kind": r[2], "label": r[3], "used": r[4], "limit": r[5]}
                          for r in con.execute("SELECT ts_utc, day, kind, label, used, limit_value FROM limit_events "
                                               "ORDER BY rowid DESC LIMIT 10")]
                con.close()
            except Exception:
                events = []
        summary = {}
        for name in PROVIDER_NAMES:
            other = sum(read_usage(ledger, provider=name).values())
            cap = self.provider_limits(name)["per_day"]
            summary[name] = {"used_today": other, "max_per_day": cap, "remaining_day": max(0, cap - other)}
        return {"provider": provider, "ledger_path": str(ledger), "ledger_exists": ledger.exists(), "ist_day": now_ist.date().isoformat(),
                "used_today": used, "by_label": today, "max_per_run": lim["per_run"], "max_per_day": lim["per_day"],
                "remaining_day": max(0, lim["per_day"] - used), "resets_at_ist": reset.isoformat(timespec="minutes"),
                "documented_account_limit": get_provider(provider).capabilities().rate_limit.per_day or DHAN_DOCUMENTED_DAILY_LIMIT,
                "history": read_usage(ledger, history=7, provider=provider) or [],
                "limit_events": events, "caveat": counter_caveat(provider, get_provider(provider).label), "providers": summary}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="stockdata-scheduler", daemon=True)
        self._thread.start()

    def shutdown(self, *, stop_workers: bool = False, wait: float = 0.0) -> list[dict]:
        """Stop the scheduler thread. Workers keep running (detached) unless ``stop_workers`` asks them to stop
        gracefully. Returns the workers that are still active."""
        if stop_workers:
            for job in self._active_jobs():
                try:
                    self.stop(job["id"], reason="pause", actor="shutdown")
                except JobError:
                    pass
            deadline = time.time() + wait
            while wait and time.time() < deadline and self._active_jobs():
                self.tick()
                time.sleep(1.0)
        self._stop_evt.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        return [self.job_view(j) for j in self._active_jobs()]

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.tick()
            except Exception:                       # the scheduler must never die
                log.exception("scheduler tick failed")
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()

    # ------------------------------------------------------------------ recovery
    def recover(self) -> None:
        """After a server restart: re-adopt live workers, finalize finished ones, flag the vanished."""
        with self._lock:
            for job in self._active_jobs():
                run = self._latest_run(job["id"])
                if run is None:
                    self._set(job["id"], status="interrupted", note="no worker record found after restart",
                              next_action="Resume to continue from the last committed window.")
                    self.store.event(job["id"], "recovery", "active job had no worker record; marked interrupted", "warning")
                    continue
                result = self._read_result(run)
                if result is not None:
                    self._finalize(job, run, result)
                elif is_alive(run["pid"], run["pid_create_time"]):
                    self.store.event(job["id"], "adopted", f"re-adopted running worker (pid {run['pid']}) after server restart")
                else:
                    self._worker_vanished(job, run, None)

    # ------------------------------------------------------------------ queries
    def _active_jobs(self) -> list[dict]:
        return self.store.all(f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(ACTIVE))}) ORDER BY id", ACTIVE)

    def get(self, job_id: int) -> dict:
        j = self.store.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not j:
            raise JobError(f"job {job_id} does not exist", 404)
        return j

    def _latest_run(self, job_id: int) -> dict | None:
        return self.store.one("SELECT * FROM job_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,))

    def writer_for(self, tf: str) -> int | None:
        r = self.store.one(f"SELECT id FROM jobs WHERE status IN ({','.join('?' * len(ACTIVE))}) AND "
                           "(timeframe = ? OR kind = 'instruments_load') LIMIT 1", (*ACTIVE, tf))
        return r["id"] if r else None

    def _set(self, job_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.store.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    # ------------------------------------------------------------------ creating jobs
    def _resolve_ids(self, spec: J.JobSpec) -> list[int] | None:
        """Validate the selection against the loaded universe; expand a saved group to security ids."""
        sel = spec.selection
        if spec.kind not in J.SESSION_KINDS or sel.mode == "all":
            return None
        master = self.data.instrument_master().get("rows", [])
        if not master:
            raise JobError("No instrument universe is loaded yet. Load the instrument master first (Instruments page).", 400)
        by_symbol = {r["symbol"].upper(): r for r in master}
        by_id = {r["security_id"]: r for r in master}
        if sel.mode == "symbols":
            missing = [s for s in sel.symbols if s not in by_symbol]
            if missing:
                raise JobError(f"symbols not in the loaded universe: {', '.join(missing[:10])}", 400)
            return None
        if sel.mode == "group":
            grp = self.store.one("SELECT * FROM symbol_groups WHERE id = ?", (sel.group_id,))
            if not grp:
                raise JobError(f"saved group {sel.group_id} does not exist", 404)
            ids = json.loads(grp["security_ids"])
        else:
            ids = sel.security_ids
        missing_ids = [i for i in ids if i not in by_id]
        if missing_ids:
            raise JobError(f"security ids not in the loaded universe: {missing_ids[:10]}", 400)
        return ids

    def create_job(self, spec: J.JobSpec, *, plan: dict | None = None, campaign_id: int | None = None,
                   campaign_pos: int | None = None, source_job_id: int | None = None, allow_duplicate: bool = False,
                   actor: str = "ui") -> dict:
        with self._lock:
            ids = self._resolve_ids(spec)
            if spec.kind in J.SESSION_KINDS:
                # The provider is fixed when the job is CREATED: changing the default later never silently moves queued work
                # to another source, and the job's provenance is explicit.
                prov = spec.provider or self.default_provider(spec.timeframe)
                adapter = get_provider(prov)
                if not adapter.supports(get_timeframe(spec.timeframe)):
                    raise JobError(f"{adapter.label} does not provide {spec.timeframe} candles", 400)
                spec = spec.model_copy(update={"provider": prov})
            fp = J.fingerprint(spec, ids)
            if not allow_duplicate:
                # only work that is still pending counts as "the same job": a FINISHED identical job (even one that completed
                # with warnings) must not block today's update or a deliberate re-run
                dup = self.store.one("SELECT id, status FROM jobs WHERE fingerprint = ? AND status NOT IN "
                                     "('completed','completed_with_warnings','cancelled')", (fp,))
                if dup:
                    raise JobError(f"An identical job already exists: #{dup['id']} ({dup['status']}). Resume or edit that one "
                                   "instead of downloading the same work twice.", 409)
            args = self._engine_args(spec, ids)
            minpos = self.store.one("SELECT COALESCE(MAX(queue_pos), 0) + 1 AS p FROM jobs")["p"]
            name = spec.name or J.default_name(spec, ids)
            options = {"spec": spec.model_dump(mode="json"), "security_ids": ids}
            cur = self.store.execute(
                """INSERT INTO jobs (name, kind, timeframe, status, options, plan, command, campaign_id, campaign_pos,
                   queue_pos, created_at, queued_at, fingerprint, source_job_id, windows_planned, instruments_total, not_before,
                   provider, lane)
                   VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, spec.kind, spec.timeframe, json.dumps(options), json.dumps(plan) if plan else None,
                 J.command_text(args), campaign_id, campaign_pos, minpos, now(), now(), fp, source_job_id,
                 plan.get("windows") if plan else None, plan.get("instruments") if plan else None,
                 spec.start_after.isoformat(timespec="seconds") if spec.start_after else None,
                 spec.provider, J.lane_of(spec.kind)))
            job_id = cur.lastrowid
            self.store.event(job_id, "created", f"job created by {actor}: {name}")
            self.store.audit("job.create", f"job:{job_id}", {"command": J.command_text(args), "canonical": J.canonical(spec, ids)})
            self.wake()
            return self.get(job_id)

    def _engine_args(self, spec: J.JobSpec, ids: list[int] | None, *, rate: float | None = None) -> list[str]:
        """The exact engine command of a job. Request limits are the PROVIDER's own; a historical job also leaves a reserve of
        the day's requests untouched so a live update can always run; a live job runs gently beside a historical one."""
        eff = self.effective()
        if spec.kind not in J.SESSION_KINDS:
            return J.engine_args(spec, data_dir=str(self.s.data_dir), security_ids=ids)
        prov = spec.provider or "dhan"
        lim = self.provider_limits(prov)
        per_day = lim["per_day"]
        if J.lane_of(spec.kind) == "historical":
            per_day = max(1, per_day - int(eff["live_reserve"]))
        if rate is None:
            rate = self._rate_for(spec, prov)
        return J.engine_args(spec, data_dir=str(self.s.data_dir), security_ids=ids, rps=rate,
                             max_per_run=min(lim["per_run"], per_day), max_per_day=per_day)

    def _rate_for(self, spec: J.JobSpec, prov: str) -> float | None:
        eff = self.effective()
        live = J.lane_of(spec.kind) == "live"
        if prov == "dhan":
            rps = eff["rps"]
            if live:
                return min(rps, LIVE_RPS)
            return min(rps, PARALLEL_RPS) if eff["parallel_timeframes"] else rps
        per_sec = get_provider(prov).capabilities().rate_limit.per_second or 1
        return per_sec * LIVE_RATE_FRACTION if live else None        # historical: the adapter's own conservative default

    def spec_of(self, job: dict) -> J.JobSpec:
        return J.JobSpec.model_validate(json.loads(job["options"])["spec"])

    # ------------------------------------------------------------------ actions
    def start_now(self, job_id: int) -> tuple[dict, str | None]:
        """Move a job to the front and start it immediately if nothing prevents it. Returns (job, reason-if-not-started)."""
        with self._lock:
            job = self.get(job_id)
            if job["status"] in ACTIVE:
                # Creating a job wakes the scheduler, which may start it before this explicit "start now" arrives; that is
                # the outcome the person asked for, not an error.
                return job, None
            if job["status"] not in ("queued", *RESUMABLE):
                raise JobError(f"a {job['status']} job cannot be started", 409)
            front = self.store.one("SELECT COALESCE(MIN(queue_pos), 1) - 1 AS p FROM jobs")["p"]
            self._set(job_id, status="queued", queue_pos=front, queued_at=now(), pending_action=None, not_before=None)
            self.store.audit("job.start_now", f"job:{job_id}", {"cleared_schedule": bool(job["not_before"])})
            job = self.get(job_id)
            reason = self._blocked_reason(job, honour_queue_switch=False)
            if reason is None:
                reason = self._try_start(job)
            self.wake()
            return self.get(job_id), reason

    def resume(self, job_id: int) -> dict:
        with self._lock:
            job = self.get(job_id)
            if job["status"] not in RESUMABLE:
                raise JobError(f"only paused, interrupted, stopped or failed jobs can be resumed (this one is {job['status']})", 409)
            if job.get("yielded_to"):
                self._set(job_id, yielded_to=None)
            front = self.store.one("SELECT COALESCE(MIN(queue_pos), 1) - 1 AS p FROM jobs")["p"]
            self._set(job_id, status="queued", queue_pos=front, queued_at=now(), pending_action=None, block_reason=None)
            self.store.event(job_id, "resumed", "resume requested; the same command continues from the last committed window")
            self.store.audit("job.resume", f"job:{job_id}", {"from": job["status"]})
            self.wake()
            return self.get(job_id)

    def pause(self, job_id: int, *, actor: str = "ui") -> dict:
        return self.stop(job_id, reason="pause", actor=actor)

    def stop(self, job_id: int, *, reason: str = "stop", actor: str = "ui") -> dict:
        """Graceful: ask the worker to finish its in-flight window and exit. reason: 'pause' (hold) or 'stop'."""
        assert reason in ("pause", "stop")
        with self._lock:
            job = self.get(job_id)
            if job["status"] == "queued":
                if reason == "stop":
                    raise JobError("this job has not started; use Cancel to remove it from the queue (or Pause to hold it)", 409)
                self._set(job_id, status="paused", note="held in the queue by the user")
                self.store.event(job_id, "paused", "held before starting")
                return self.get(job_id)
            if job["status"] not in ACTIVE:
                raise JobError(f"a {job['status']} job has no worker to {reason}", 409)
            run = self._latest_run(job_id)
            if run is None:
                raise JobError("no worker is recorded for this job", 409)
            ctl = jobcontrol.JobControl(run["control_dir"])
            ctl.request_stop(reason)
            self._set(job_id, status="stopping", pending_action=reason,
                      next_action="Waiting for the in-flight window to finish (an API call already sent is never abandoned).")
            self.store.event(job_id, f"{reason}_requested", f"graceful {reason} requested by {actor}; the worker stops after the in-flight window is committed")
            self.store.audit(f"job.{reason}", f"job:{job_id}", {"actor": actor})
            return self.get(job_id)

    def kill(self, job_id: int) -> dict:
        """Hard stop. Committed windows are safe; the in-flight window is requested again on resume."""
        with self._lock:
            job = self.get(job_id)
            if job["status"] not in ACTIVE:
                raise JobError("only a running job can be force-stopped", 409)
            run = self._latest_run(job_id)
            outcome = hard_kill(run["pid"], run["pid_create_time"]) if run else "gone"
            result = self._read_result(run) if run else None
            if result is not None:
                self._finalize(job, run, result)
            else:
                self._worker_vanished(job, run, f"force-stopped by the user ({outcome})")
            self.store.audit("job.kill", f"job:{job_id}", {"outcome": outcome})
            return self.get(job_id)

    def cancel(self, job_id: int, *, abandon: bool = False) -> dict:
        with self._lock:
            job = self.get(job_id)
            if job["status"] in ACTIVE:
                raise JobError("stop the running job first (Pause or Stop), then it can be abandoned", 409)
            if job["status"] == "queued" or (job["status"] == "paused" and job["runs"] == 0):
                self._set(job_id, status="cancelled", finished_at=now(), note="cancelled before it started", next_action=None)
            elif job["status"] in RESUMABLE and abandon:
                self._set(job_id, status="cancelled", finished_at=now(), next_action=None,
                          note=f"remaining work abandoned by the user (status was {job['status']}); downloaded data is kept")
            else:
                raise JobError("only jobs that have not started can be cancelled; use Abandon for a partly done job", 409)
            self.store.event(job_id, "cancelled", "cancelled by the user")
            self.store.audit("job.cancel", f"job:{job_id}", {"abandon": abandon})
            self.wake()
            return self.get(job_id)

    def delete(self, job_id: int) -> None:
        with self._lock:
            job = self.get(job_id)
            if job["status"] not in ("completed", "completed_with_warnings", "cancelled"):
                raise JobError("only finished or cancelled jobs can be removed from the history", 409)
            self.store.execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
            self.store.execute("DELETE FROM job_runs WHERE job_id = ?", (job_id,))
            self.store.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self.store.audit("job.delete", f"job:{job_id}", {"name": job["name"]})

    def reorder(self, ids: list[int]) -> list[dict]:
        with self._lock:
            queued = {j["id"] for j in self.store.all("SELECT id FROM jobs WHERE status = 'queued'")}
            if not set(ids) <= queued:
                raise JobError("only queued jobs can be reordered", 409)
            for pos, jid in enumerate(ids, 1):
                self._set(jid, queue_pos=float(pos))
            self.store.audit("queue.reorder", None, {"order": ids})
            return self.list_jobs()

    def retry_failed(self, job_id: int, *, security_ids: list[int] | None = None, empty_error_codes: list[str] | None = None,
                     confirm_full: bool = False) -> dict:
        """A NEW job that reruns the same selection with the engine's --retry-failed (optionally recording reviewed
        DH-907 windows as empty). Nothing else changes: completed windows are never requested again."""
        with self._lock:
            job = self.get(job_id)
            if job["kind"] not in ("download", "update"):
                raise JobError("only download and update jobs have failures to retry", 400)
            spec = self.spec_of(job)
            sel = spec.selection
            if security_ids:
                sel = J.Selection(mode="security_ids", security_ids=security_ids)
            new = spec.model_copy(update={"selection": sel, "retry_failed": True, "name": None,
                                          "empty_error_codes": empty_error_codes or [],
                                          "confirm_full": confirm_full or spec.confirm_full})
            new = J.JobSpec.model_validate(new.model_dump(mode="json"))
            return self.create_job(new, source_job_id=job_id, actor="retry", allow_duplicate=True)

    # ------------------------------------------------------------------ scheduling
    def _blocked_reason(self, job: dict, *, honour_queue_switch: bool = True, ignore_historical_writers: bool = False) -> str | None:
        eff = self.effective()
        if honour_queue_switch and not eff["queue_enabled"]:
            return "queue processing is switched off"
        if job["not_before"] and datetime.fromisoformat(job["not_before"]) > datetime.now(timezone.utc):
            when = datetime.fromisoformat(job["not_before"]).astimezone(IST)
            return f"scheduled: starts no earlier than {when:%Y-%m-%d %H:%M} IST"
        for tf_name in (["daily", "15min"] if job["kind"] == "instruments_load" else [job["timeframe"]]):
            label = self.gate.maintenance(tf_name)
            if label:
                return f"the {tf_name} database is in maintenance ({label})"
        if job["campaign_id"]:
            camp = self.store.one("SELECT * FROM campaigns WHERE id = ?", (job["campaign_id"],))
            ok = FINISHED_OK if camp and camp["continue_on_warnings"] else ("completed",)
            for prev in self.store.all("SELECT id, status FROM jobs WHERE campaign_id = ? AND campaign_pos < ? ORDER BY campaign_pos",
                                       (job["campaign_id"], job["campaign_pos"])):
                if prev["status"] not in ok:
                    return f"campaign order: waiting for job #{prev['id']} ({prev['status']})"
        active = self._active_jobs()
        lane = J.lane_of(job["kind"])
        if job["kind"] == "instruments_load":
            if active:
                return f"waiting for running job #{active[0]['id']} to finish (the instrument master update writes both databases)"
        else:
            for a in active:
                if a["kind"] == "instruments_load":
                    return "the instrument master is being updated"
                if a["timeframe"] == job["timeframe"]:
                    if ignore_historical_writers and J.lane_of(a["kind"]) == "historical":
                        continue
                    return f"job #{a['id']} is writing the {job['timeframe']} database (one writer per database)"
            # each lane has its own allowance: a long historical backfill never occupies the live lane's slot
            same_lane = [a for a in active if J.lane_of(a["kind"]) == lane and not (ignore_historical_writers and lane == "historical")]
            limit = 2 if (eff["parallel_timeframes"] and lane == "historical") else 1
            if len(same_lane) >= limit:
                what = "live updates" if lane == "live" else "downloads"
                return f"waiting for job #{same_lane[0]['id']} ({what} run one at a time in their lane)"
        if job["kind"] in J.SESSION_KINDS:
            prov = job.get("provider") or "dhan"
            problem = self.provider_ready(prov)
            if problem:
                return problem
            if self.usage(prov)["remaining_day"] <= 0:
                return ("the daily request budget is used up (resets at IST midnight)" if prov == "dhan"
                        else f"the daily request budget for {get_provider(prov).label} is used up (resets at IST midnight)")
        if free_bytes(self.s.data_dir) < max(MIN_FREE_BYTES, int(eff["min_free_gb"] * 1024 ** 3) // 2):
            return "not enough free disk space"
        return None

    def _eligible_queue(self) -> list[dict]:
        queued = self.store.all("SELECT * FROM jobs WHERE status = 'queued' ORDER BY queue_pos, id")
        return sorted(queued, key=lambda j: 0 if J.lane_of(j["kind"]) == "live" else 1)       # live updates are looked at first

    def _yield_for_live(self, job: dict) -> bool:
        """A live job that only waits for a HISTORICAL writer on its database asks that job to pause at its next checkpoint;
        the historical job resumes by itself when the live job is done. Returns True when a pause was requested."""
        if J.lane_of(job["kind"]) != "live" or self._blocked_reason(job, ignore_historical_writers=True) is not None:
            return False
        asked = False
        for a in self._active_jobs():
            if (a["timeframe"] == job["timeframe"] and J.lane_of(a["kind"]) == "historical" and a["status"] in ("preparing", "running")):
                try:
                    self.pause(a["id"], actor="scheduler")
                except JobError:
                    continue
                self._set(a["id"], yielded_to=job["id"])
                self.store.event(a["id"], "yielded", f"pausing at the next checkpoint to let live update #{job['id']} run; it resumes automatically afterwards")
                asked = True
        if asked:
            self._set(job["id"], block_reason="waiting for a historical download to pause at a checkpoint (live updates take priority)")
        return asked

    def _resume_yielded(self) -> None:
        for j in self.store.all("SELECT * FROM jobs WHERE yielded_to IS NOT NULL AND status = 'paused'"):
            live = self.store.one("SELECT status FROM jobs WHERE id = ?", (j["yielded_to"],))
            if live is None or live["status"] not in ("queued",) + ACTIVE:
                other = j["yielded_to"]
                self._set(j["id"], yielded_to=None)
                self.resume(j["id"])
                self.store.event(j["id"], "auto_resumed", f"resumed automatically: live update #{other} is no longer running")

    def tick(self) -> None:
        with self._lock:
            self._reap()
            self._resume_yielded()
            eff = self.effective()
            if eff["auto_resume_budget"]:
                today = datetime.now(IST).date().isoformat()
                for j in self.store.all("SELECT * FROM jobs WHERE status = 'stopped_by_limit'"):
                    fin = _dt(j["finished_at"])
                    if fin and fin.astimezone(IST).date().isoformat() < today:
                        self._set(j["id"], status="queued", queued_at=now())
                        self.store.event(j["id"], "auto_resumed", "request budget reset; resumed automatically")
            for job in self._eligible_queue():
                reason = self._blocked_reason(job)
                if reason is None:
                    reason = self._try_start(job)
                elif self._yield_for_live(job):
                    continue
                if reason != job["block_reason"]:
                    self._set(job["id"], block_reason=reason)

    def _try_start(self, job: dict) -> str | None:
        """Returns None when started, else the reason why not."""
        tf_names = ["daily", "15min"] if job["kind"] == "instruments_load" else [job["timeframe"]]
        claimed = []
        try:
            for tf in tf_names:
                if not self.gate.claim_for_writer(tf, timeout=3.0):
                    for c in claimed:
                        self.gate.release_claim(c)
                    return f"waiting for a data view/report on the {tf} database to finish"
                claimed.append(tf)
            return self._spawn(job)
        finally:
            for tf in claimed:
                self.gate.release_claim(tf)

    def _spawn(self, job: dict) -> str | None:
        spec = self.spec_of(job)
        ids = json.loads(job["options"]).get("security_ids")
        seq = job["runs"] + 1
        run_dir = self.s.jobs_dir / str(job["id"]) / f"run_{seq}"
        run_dir.mkdir(parents=True, exist_ok=True)
        args = self._engine_args(spec, ids)
        argv = [sys.executable, "-m", "stockdata.web.worker", "--control-dir", str(run_dir), "--", *args]
        env = {k: v for k, v in os.environ.items() if k not in (*all_provider_keys(), jobcontrol.ENV_VAR)}
        if spec.kind in J.SESSION_KINDS:                                  # a worker gets ONLY its own provider's credentials
            env.update(self.creds.provider_env(spec.provider or "dhan"))
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        log_path = run_dir / "worker.log"
        try:
            proc, created = spawn_detached(argv, cwd=self.s.repo_root, env=env, log_path=log_path)
        except OSError as exc:
            self.store.event(job["id"], "spawn_failed", f"could not start the worker: {exc}", "error")
            return f"could not start the worker: {redact(str(exc))}"
        cur = self.store.execute(
            """INSERT INTO job_runs (job_id, seq, started_at, pid, pid_create_time, control_dir, log_path, argv)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (job["id"], seq, now(), proc.pid, created, str(run_dir), str(log_path), J.command_text(args)))
        self._procs[cur.lastrowid] = proc
        self._set(job["id"], status="preparing", runs=seq, started_at=job["started_at"] or now(), finished_at=None,
                  last_activity_at=now(), pending_action=None, block_reason=None, exit_code=None, note=None,
                  last_error=None, next_action=None, command=J.command_text(args))
        self.store.event(job["id"], "started", f"worker started (pid {proc.pid}, run {seq})")
        self.store.audit("job.start", f"job:{job['id']}", {"run": seq, "command": J.command_text(args), "pid": proc.pid})
        return None

    # ------------------------------------------------------------------ supervision
    def _read_result(self, run: dict) -> dict | None:
        try:
            return json.loads((Path(run["control_dir"]) / "result.json").read_text())
        except (OSError, ValueError):
            return None

    def _log_tail(self, run: dict, lines: int = 80) -> str:
        try:
            data = Path(run["log_path"]).read_bytes()[-40_000:].decode("utf-8", "replace")
            return redact("\n".join(data.splitlines()[-lines:]))
        except OSError:
            return ""

    def _reap(self) -> None:
        for job in self._active_jobs():
            run = self._latest_run(job["id"])
            if run is None:                         # an active job with no worker record can never finish by itself
                self._set(job["id"], status="interrupted", pending_action=None, finished_at=now(),
                          note="no worker record was found for this job",
                          next_action="Resume to continue from the last committed window.")
                self.store.event(job["id"], "recovery", "active job had no worker record; marked interrupted", "warning")
                continue
            progress = jobcontrol.read_progress(run["control_dir"])
            if progress:
                self._apply_progress(job, run, progress)
            result = self._read_result(run)
            if result is not None:
                self._finalize(job, run, result)
                continue
            proc = self._procs.get(run["id"])
            exited = proc is not None and proc.poll() is not None
            if exited:
                time.sleep(0.3)                      # let a result file land
                result = self._read_result(run)
                if result is not None:
                    self._finalize(job, run, result)
                else:
                    self._worker_vanished(job, run, f"worker exited unexpectedly (exit code {proc.returncode})", proc.returncode)
            elif not is_alive(run["pid"], run["pid_create_time"]):
                time.sleep(0.3)
                result = self._read_result(run)
                if result is not None:
                    self._finalize(job, run, result)
                else:
                    self._worker_vanished(job, run, None)

    def _apply_progress(self, job: dict, run: dict, p: dict) -> None:
        prior = (json.loads(job["progress"]) if job["progress"] else {}).get("prior", {"windows": 0, "rows": 0, "attempts": 0})
        c = p.get("counts", {})
        plan = p.get("plan") or {}
        fields = {
            "windows_done": prior["windows"] + c.get("windows_done", 0),
            "windows_planned": prior["windows"] + plan["windows"] if plan.get("windows") is not None else job["windows_planned"],
            "instruments_total": plan.get("instruments", job["instruments_total"]),
            "instruments_done": c.get("instruments_done", 0),
            "rows_inserted": prior["rows"] + c.get("rows_inserted", 0),
            "attempts_used": prior["attempts"] + p.get("requests", 0),
            "failed_windows": c.get("failed", 0), "retriable_windows": c.get("retriable", 0),
            "last_activity_at": p.get("updated_at") and (p["updated_at"] + "+00:00") or job["last_activity_at"],
            "progress": json.dumps({"prior": prior, "run": p}),
        }
        if p.get("last_error"):
            fields["last_error"] = redact(p["last_error"])
        if job["status"] == "preparing" and p.get("phase") == "running":
            fields["status"] = "running"
        self._set(job["id"], **fields)

    def _finalize(self, job: dict, run: dict, result: dict) -> None:
        text = self._log_tail(run, 200) + "\n" + (result.get("message") or "")
        code = result.get("exit_code")
        status, engine_status, hint = J.classify_exit(code, pending_action=job["pending_action"], log_text=text)
        note = None
        for line in reversed(text.splitlines()):
            if line.startswith("note: "):
                note = line[6:]
                break
        prog = json.loads(job["progress"]) if job["progress"] else {}
        p = jobcontrol.read_progress(run["control_dir"]) or prog.get("run") or {}
        c = p.get("counts", {})
        prior = prog.get("prior", {"windows": 0, "rows": 0, "attempts": 0})
        new_prior = {"windows": prior["windows"] + c.get("windows_done", 0), "rows": prior["rows"] + c.get("rows_inserted", 0),
                     "attempts": prior["attempts"] + p.get("requests", 0)}
        self.store.execute("UPDATE job_runs SET ended_at = ?, exit_code = ?, engine_status = ?, outcome = ?, attempts_used = ? "
                           "WHERE id = ?", (now(), code, engine_status, status, p.get("requests", 0), run["id"]))
        self._set(job["id"], status=status, engine_status=engine_status, exit_code=code, finished_at=now(), pending_action=None,
                  next_action=hint, note=redact(note or (result.get("message") or "")[:400]) or None,
                  windows_done=new_prior["windows"], rows_inserted=new_prior["rows"], attempts_used=new_prior["attempts"],
                  failed_windows=c.get("failed", 0), retriable_windows=c.get("retriable", 0),
                  progress=json.dumps({"prior": new_prior, "run": p}),
                  last_error=redact(p.get("last_error") or "") or job["last_error"])
        self._procs.pop(run["id"], None)
        level = "info" if status in ("completed", "paused", "stopped") else "warning" if status != "failed" else "error"
        self.store.event(job["id"], "finished", f"worker ended with exit code {code}: {status}" + (f" - {note}" if note else ""), level)

    def _worker_vanished(self, job: dict, run: dict | None, why: str | None, code: int | None = None) -> None:
        msg = why or "the worker process is gone and left no result (crash, kill, reboot or power loss)"
        prog = json.loads(job["progress"]) if job["progress"] else {}
        p = (jobcontrol.read_progress(run["control_dir"]) if run else None) or prog.get("run") or {}
        c = p.get("counts", {})
        prior = prog.get("prior", {"windows": 0, "rows": 0, "attempts": 0})
        new_prior = {"windows": prior["windows"] + c.get("windows_done", 0), "rows": prior["rows"] + c.get("rows_inserted", 0),
                     "attempts": prior["attempts"] + p.get("requests", 0)}
        if run:
            self.store.execute("UPDATE job_runs SET ended_at = ?, exit_code = ?, outcome = 'interrupted' WHERE id = ?",
                               (now(), code, run["id"]))
            self._procs.pop(run["id"], None)
        self._set(job["id"], status="interrupted", finished_at=now(), pending_action=None, exit_code=code,
                  note=msg, windows_done=new_prior["windows"], rows_inserted=new_prior["rows"],
                  attempts_used=new_prior["attempts"], progress=json.dumps({"prior": new_prior, "run": p}),
                  next_action="Resume: the engine continues from the last committed window; the in-flight window is requested again.")
        self.store.event(job["id"], "interrupted", msg, "warning")

    # ------------------------------------------------------------------ views
    def list_jobs(self, statuses: list[str] | None = None, limit: int = 500) -> list[dict]:
        q, params = "SELECT * FROM jobs", []
        if statuses:
            q += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params = list(statuses)
        q += " ORDER BY CASE WHEN status IN ('preparing','running','stopping') THEN 0 WHEN status = 'queued' THEN 1 ELSE 2 END, " \
             "queue_pos, id DESC LIMIT ?"
        return [self.job_view(j) for j in self.store.all(q, (*params, limit))]

    def counts(self) -> dict:
        rows = self.store.all("SELECT status, count(*) AS n FROM jobs GROUP BY status")
        out = {r["status"]: r["n"] for r in rows}
        return {"queued": out.get("queued", 0), "running": sum(out.get(s, 0) for s in ACTIVE),
                "paused": out.get("paused", 0),
                "interrupted": out.get("interrupted", 0) + out.get("stopped", 0) + out.get("stopped_by_limit", 0),
                "failed": out.get("failed", 0), "completed": out.get("completed", 0) + out.get("completed_with_warnings", 0),
                "total": sum(out.values()), "by_status": out}

    def job_view(self, job: dict) -> dict:
        prog = json.loads(job["progress"]) if job["progress"] else {}
        run_p = prog.get("run") or {}
        counts = run_p.get("counts", {})
        run = self._latest_run(job["id"])
        active = job["status"] in ACTIVE
        alive = is_alive(run["pid"], run["pid_create_time"]) if (run and active) else False
        updated = run_p.get("updated_at")
        heartbeat_age = None
        if updated and active:
            heartbeat_age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(updated + "+00:00")).total_seconds())
        stale = bool(active and alive and heartbeat_age is not None and heartbeat_age > STALE_AFTER)
        planned, done = job["windows_planned"], job["windows_done"]
        eta = None
        if active and run_p.get("plan") and counts.get("windows_done", 0) >= 20:
            started = datetime.fromisoformat(run_p["started_at"] + "+00:00")
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            remaining = max(0, run_p["plan"]["windows"] - counts["windows_done"])
            if elapsed >= 60:
                eta = {"seconds": int(remaining / (counts["windows_done"] / elapsed)), "basis": "observed rate of this run; an estimate"}
        view = {k: job[k] for k in ("id", "name", "kind", "timeframe", "status", "campaign_id", "campaign_pos", "created_at",
                                    "queued_at", "started_at", "finished_at", "last_activity_at", "runs", "exit_code",
                                    "engine_status", "note", "last_error", "next_action", "pending_action", "block_reason",
                                    "windows_planned", "windows_done", "instruments_total", "instruments_done",
                                    "rows_inserted", "attempts_used", "failed_windows", "retriable_windows", "command",
                                    "source_job_id")}
        spec = self.spec_of(job)
        sel = spec.selection
        session_kind = job["kind"] in J.SESSION_KINDS
        view.update({
            "provider": (job.get("provider") or "dhan") if session_kind else None,
            "lane": job.get("lane") or J.lane_of(job["kind"]),
            "workflow": {"download": "Historical download", "refresh": "Historical repair", "instruments_load": "Instrument master",
                         "update": "Live update (post-close)", "poll": "Live polling (current session)"}[job["kind"]],
            "yielded_to": job.get("yielded_to"),
            "live": ({"overlap_days": spec.overlap_days, "seed_days": spec.seed_days, "poll_seconds": spec.poll_seconds,
                      "max_cycles": spec.max_cycles} if job["kind"] in J.LIVE_KINDS else None),
            "allow_cross_provider": spec.allow_cross_provider if job["kind"] == "refresh" else None,
            "scheduled_for": job["not_before"] if job["status"] == "queued" else None,
            "selection": None if sel is None else {"mode": sel.mode, "count": None if sel.mode == "all" else
                         (len(sel.symbols) if sel.mode == "symbols" else len(json.loads(job["options"]).get("security_ids") or [])),
                         "symbols": sel.symbols[:20], "limit": sel.limit},
            "range": {"start": spec.start_date.isoformat() if spec.start_date else None,
                      "end": spec.end_date.isoformat() if spec.end_date else None},
            "retry_failed": spec.retry_failed, "empty_error_codes": spec.empty_error_codes,
            "plan": json.loads(job["plan"]) if job["plan"] else None,
            "progress": {"windows_done": done, "windows_planned": planned,
                         "fraction": (done / planned) if planned else None,
                         "current": {"instrument": run_p.get("instrument"), "window": run_p.get("window"),
                                     "phase": run_p.get("phase")} if active else None,
                         "run_counts": counts, "eta": eta},
            "worker": {"pid": run["pid"] if run else None, "alive": alive, "stale": stale,
                       "heartbeat_age_seconds": heartbeat_age, "run_seq": run["seq"] if run else None,
                       "last_checkpoint": run_p.get("updated_at")} if run else None,
            "actions": self.actions_for(job),
        })
        for k in ("note", "last_error", "next_action", "block_reason", "command"):
            view[k] = redact(view[k]) if view[k] else view[k]
        return view

    def actions_for(self, job: dict) -> list[str]:
        s = job["status"]
        acts = {"queued": ["start", "pause", "cancel"], "preparing": ["pause", "stop", "kill"], "running": ["pause", "stop", "kill"],
                "stopping": ["kill"], "paused": ["resume", "start", "abandon"],
                "interrupted": ["resume", "start", "abandon"], "stopped": ["resume", "start", "abandon"],
                "stopped_by_limit": ["resume", "start", "abandon"], "failed": ["resume", "start", "abandon"],
                "completed_with_warnings": ["retry_failed", "delete"], "completed": ["delete"], "cancelled": ["delete"]}[s]
        if job["kind"] in ("download", "update") and s in ("failed", "interrupted", "stopped", "paused") and (job["failed_windows"] or job["retriable_windows"]):
            acts = acts + ["retry_failed"]
        if job["kind"] not in ("download", "update"):
            acts = [a for a in acts if a != "retry_failed"]
        return acts

    # ------------------------------------------------------------------ logs
    def read_log(self, job_id: int, *, run_seq: int | None = None, tail: int = 200, offset: int | None = None) -> dict:
        job = self.get(job_id)
        run = (self.store.one("SELECT * FROM job_runs WHERE job_id = ? AND seq = ?", (job_id, run_seq))
               if run_seq else self._latest_run(job_id))
        if run is None:
            return {"job_id": job_id, "text": "", "offset": 0, "run_seq": None, "runs": job["runs"]}
        path = Path(run["log_path"])
        try:
            size = path.stat().st_size
        except OSError:
            return {"job_id": job_id, "text": "", "offset": 0, "run_seq": run["seq"], "runs": job["runs"]}
        if offset is not None and 0 <= offset <= size:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(400_000)
            return {"job_id": job_id, "text": redact(data.decode("utf-8", "replace")), "offset": offset + len(data),
                    "run_seq": run["seq"], "runs": job["runs"], "size": size}
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 400_000))
            data = fh.read()
        lines = data.decode("utf-8", "replace").splitlines()[-tail:]
        return {"job_id": job_id, "text": redact("\n".join(lines)), "offset": size, "run_seq": run["seq"], "runs": job["runs"], "size": size}

    def log_path(self, job_id: int, run_seq: int | None = None) -> Path | None:
        run = (self.store.one("SELECT * FROM job_runs WHERE job_id = ? AND seq = ?", (job_id, run_seq))
               if run_seq else self._latest_run(job_id))
        return Path(run["log_path"]) if run else None

    # ------------------------------------------------------------------ plan preview
    def preview(self, spec: J.JobSpec) -> dict:
        """The engine's own planner (read-only) for any job kind, plus budget, duration, disk and conflict information."""
        if spec.kind not in J.SESSION_KINDS:
            raise JobError("only downloads, updates, polls and repairs have a request plan", 400)
        ids = self._resolve_ids(spec)
        tf = get_timeframe(spec.timeframe)
        prov = spec.provider or self.default_provider(spec.timeframe)
        adapter = get_provider(prov)
        if not adapter.supports(tf):
            raise JobError(f"{adapter.label} does not provide {spec.timeframe} candles", 400)
        spec = spec.model_copy(update={"provider": prov})
        path = self.data.path(spec.timeframe)
        if not path.exists():
            raise DatabaseMissing(f"The {spec.timeframe} database does not exist yet ({path}). Load the instrument master first "
                                  "(Instruments page -> Refresh instrument master).")
        sel = spec.selection
        extra: dict = {}
        with self.gate.reader(spec.timeframe):
            with CandleDB(tf, path, read_only=True) as db:
                try:
                    if spec.kind == "download":
                        plan = build_download_plan(
                            db, settings=self.s.engine, symbols=sel.symbols if sel.mode == "symbols" else None,
                            security_ids=ids if sel.mode in ("security_ids", "group") else None,
                            select_all=sel.mode == "all", limit=sel.limit, start_date=spec.start_date, end_date=spec.end_date,
                            retry_failed=spec.retry_failed, empty_error_codes=frozenset(spec.empty_error_codes),
                            persist_history_start=False, provider=prov, foreign_empty_is_gap=spec.fill_provider_gaps)
                        requests, instruments, with_work = plan.requests, len(plan.instruments), plan.instruments_with_work
                        opts = plan.opts
                        extra = {"resolved_start": f"{opts.history_start:%Y-%m-%d}", "end_exclusive": f"{opts.cutoff:%Y-%m-%d}",
                                 "window_days": opts.window_days, "skipped_failed": plan.skipped_failed, "notes": plan.notes,
                                 "unmapped": [i.symbol for i in plan.unmapped][:20], "unmapped_count": len(plan.unmapped),
                                 "clamped_from": f"{plan.clamped_from:%Y-%m-%d}" if plan.clamped_from else None,
                                 "sample": [{"symbol": p.instrument.symbol, "security_id": p.instrument.security_id, "windows": p.requests,
                                             "first_from": f"{p.windows[-1].start:%Y-%m-%d}" if p.windows else None,
                                             "last_to": f"{p.windows[0].end:%Y-%m-%d}" if p.windows else None} for p in plan.per_instrument[:15]]}
                        rows_upper = (plan.instruments_with_work * DAILY_ROWS_UPPER if tf.name == "daily"
                                      else requests * INTRADAY_ROWS_PER_WINDOW)
                    elif spec.kind == "update":
                        insts = select_instruments(db, symbols=sel.symbols if sel.mode == "symbols" else None,
                                                   security_ids=ids if sel.mode in ("security_ids", "group") else None,
                                                   select_all=sel.mode == "all", limit=sel.limit)
                        uplan = live.plan_update(db, adapter, insts, live.UpdateOptions(
                            provider=prov, overlap_days=spec.overlap_days if spec.overlap_days is not None else live.config.DEFAULT_OVERLAP_DAYS,
                            seed_days=spec.seed_days, retry_failed=spec.retry_failed))
                        requests, instruments, with_work = uplan.requests, len(insts), len(uplan.to_update)
                        extra = {"end_exclusive": f"{uplan.cutoff:%Y-%m-%d}", "update": uplan.summary(), "notes": uplan.notes,
                                 "window_days": adapter.window_days(tf),
                                 "needs_backfill": [i.symbol for i in uplan.needs_backfill][:20],
                                 "unmapped": [i.symbol for i in uplan.unmapped][:20], "unmapped_count": len(uplan.unmapped)}
                        rows_upper = requests * (INTRADAY_ROWS_PER_WINDOW if tf.is_intraday else 40)
                    elif spec.kind == "poll":
                        insts = select_instruments(db, symbols=sel.symbols if sel.mode == "symbols" else None,
                                                   security_ids=ids if sel.mode in ("security_ids", "group") else None,
                                                   select_all=sel.mode == "all", limit=sel.limit)
                        mapped = [i for i in insts if adapter.map_instrument(i) is not None]
                        cycles = int(((live.CLOSE_GRACE_MINUTES + 375) * 60) / spec.poll_seconds) + 1
                        cycles = min(cycles, spec.max_cycles) if spec.max_cycles else cycles
                        requests, instruments, with_work = len(mapped) * cycles, len(insts), len(mapped)
                        extra = {"per_cycle": len(mapped), "cycles": cycles, "poll_seconds": spec.poll_seconds,
                                 "unmapped": [i.symbol for i in insts if i not in mapped][:20],
                                 "unmapped_count": len(insts) - len(mapped),
                                 "notes": ["Provisional bars only: repeated polling of delayed historical candles, not a live stream. "
                                           "The canonical candles stay final-only."]}
                        rows_upper = len(mapped) * 30
                    else:  # refresh
                        insts = select_instruments(db, symbols=sel.symbols if sel.mode == "symbols" else None,
                                                   security_ids=ids if sel.mode in ("security_ids", "group") else None, select_all=False)
                        start = to_midnight(spec.start_date)
                        cutoff = cutoff_exclusive(datetime.now(IST), live.config.FINALIZE_AFTER_HOUR_IST)
                        end = to_midnight(spec.end_date) if spec.end_date else cutoff
                        if end > cutoff:
                            raise JobError(f"the end date is later than the last final day boundary {cutoff:%Y-%m-%d} (exclusive)", 400)
                        days = adapter.window_days(tf)
                        first = adapter.earliest(tf)
                        if first is not None and start < to_midnight(first):
                            raise JobError(f"{adapter.label} serves {tf.name} candles only from {first}; choose a later start", 400)
                        ropts = RefreshOptions(start=start, end=end, window_days=days, provider=prov,
                                               allow_cross_provider=spec.allow_cross_provider, retry_failed=spec.retry_failed,
                                               fill_only=spec.refresh_mode == "fill_gaps")
                        requests = 0
                        for i in insts:
                            requests += len(Refresher(db, None, provider=adapter).plan(i, ropts)[1])
                        instruments, with_work = len(insts), len(insts)
                        extra = {"resolved_start": f"{start:%Y-%m-%d}", "end_exclusive": f"{end:%Y-%m-%d}", "window_days": days,
                                 "notes": (["Fill gaps: the range is re-requested and only candles that are MISSING are added. A stored candle is never "
                                            "replaced; where the provider's value differs the difference is recorded as a disagreement."]
                                           if spec.refresh_mode == "fill_gaps" else
                                           ["A repair re-requests the range and REPLACES stored candles that differ, after validating the "
                                            "response; every replaced value is kept in the audit trail."])}
                        rows_upper = requests * (INTRADAY_ROWS_PER_WINDOW if tf.is_intraday else DAILY_ROWS_UPPER)
                except (PlanError, SystemExit) as exc:
                    raise JobError(str(exc), 400)
                stored_pin = db.get_meta("history_start") if tf.is_intraday else None
                db_rows = db.con.execute(f"SELECT count(*) FROM {tf.table}").fetchone()[0]
        return self._finish_preview(spec, adapter, tf, path, requests, instruments, with_work, rows_upper, db_rows, stored_pin,
                                    ids, extra)

    def _finish_preview(self, spec, adapter, tf, path, requests, instruments, with_work, rows_upper, db_rows, stored_pin, ids, extra) -> dict:
        eff = self.effective()
        prov = adapter.name
        lane = J.lane_of(spec.kind)
        lim = self.provider_limits(prov)
        caps = adapter.capabilities()
        rps = self._rate_for(spec, prov)
        if prov == "dhan":
            rps = rps or eff["rps"]
            sustained = rps
        else:
            sustained = caps.rate_limit.sustained_per_second(LIVE_RATE_FRACTION if lane == "live" else 0.6)
        usage = self.usage(prov)
        reserve = int(eff["live_reserve"]) if lane == "historical" else 0
        room = max(0, min(lim["per_run"], usage["remaining_day"] - reserve))
        bytes_per_row, basis = DEFAULT_BYTES_PER_ROW, "synthetic benchmark of 77 bytes/row (no real-data evidence yet)"
        info = self.data.file_info(spec.timeframe)
        if db_rows >= 1_000_000:
            bytes_per_row = max(1, info["size_bytes"] // db_rows)
            basis = f"this database: {info['size_bytes']:,} bytes / {db_rows:,} rows"
        rows_basis = ("at most 6,300 candles per instrument (largest history seen: 6,159)" if tf.name == "daily" and spec.kind == "download"
                      else f"at most {INTRADAY_ROWS_PER_WINDOW:,} candles per window")
        est_bytes = int(rows_upper * bytes_per_row * 1.3)
        free = free_bytes(self.s.data_dir)
        warnings: list[dict] = []
        cred = self.creds.status() if prov == "dhan" else None
        problem = self.provider_ready(prov)
        if problem:
            expired = "expired" in problem
            warnings.append({"level": "blocker", "type": "token_expired" if expired else "credentials",
                             "message": (f"The {caps.label} credential has expired. Update it before starting." if prov != "dhan" and expired
                                         else "The Dhan access token has expired. Update it before starting." if expired
                                         else f"{caps.label} credentials are not configured ({'Settings -> Credentials' if prov == 'dhan' else 'Providers screen'}). "
                                              "The job cannot start without them.")})
        elif prov == "dhan" and cred["access_token"].get("seconds_left") is not None and sustained and requests / sustained > cred["access_token"]["seconds_left"]:
            warnings.append({"level": "warning", "type": "token_expiry",
                             "message": f"The access token expires in {cred['access_token']['seconds_left'] // 60} minutes, before this plan "
                                        f"({int(requests / sustained) // 60} minutes) can finish. The run will abort cleanly at expiry; update the token and Resume."})
        if prov != "dhan" and caps.auth.interactive_login:
            warnings.append({"level": "info", "type": "interactive_login",
                             "message": f"{caps.label} needs a fresh login to renew its token, so a long or unattended run can stop for 'action required'."})
        if requests > room:
            reserve_note = f", leaving {reserve:,} for live updates" if reserve else ""
            warnings.append({"level": "warning", "type": "budget",
                             "message": f"The plan needs {requests:,} attempts but only {room:,} fit now (per-run cap {lim['per_run']:,}, "
                                        f"{usage['remaining_day']:,} left today{reserve_note}). It will stop safely at the limit and can be resumed later."})
        if free < est_bytes * 2:
            warnings.append({"level": "warning" if free > est_bytes else "blocker", "type": "disk",
                             "message": f"Free disk {free / 1e9:.1f} GB vs a conservative estimate of {est_bytes / 1e9:.1f} GB of new data (keep 2x free)."})
        for a in [j for j in self._active_jobs() if j["timeframe"] == spec.timeframe or j["kind"] == "instruments_load"]:
            same_lane = J.lane_of(a["kind"]) == lane
            msg = (f"Job #{a['id']} is currently writing the {spec.timeframe} database; a new job will wait for it." if same_lane or lane == "historical"
                   else f"Historical job #{a['id']} is writing the {spec.timeframe} database: it will be paused at its next checkpoint "
                        "so this live update can run, then resumed automatically.")
            warnings.append({"level": "warning" if same_lane or lane == "historical" else "info", "type": "writer_conflict", "message": msg})
        dup = self.store.one("SELECT id, status FROM jobs WHERE fingerprint = ? AND status NOT IN ('completed','completed_with_warnings','cancelled')",
                             (J.fingerprint(spec, ids),))
        if dup:
            warnings.append({"level": "blocker", "type": "duplicate", "message": f"An identical job already exists: #{dup['id']} ({dup['status']})."})
        if not eff["queue_enabled"]:
            warnings.append({"level": "info", "type": "queue_off", "message": "Queue processing is switched off; queued jobs wait until you switch it on or use Start Now."})
        if extra.get("skipped_failed"):
            warnings.append({"level": "info", "type": "skipped_failed",
                             "message": f"{extra['skipped_failed']:,} previously failed window(s) will be skipped (use retry-failed to include them)."})
        if extra.get("unmapped_count"):
            warnings.append({"level": "warning", "type": "unmapped",
                             "message": f"{extra['unmapped_count']:,} instrument(s) have no {caps.label} identifier and are skipped "
                                        f"({', '.join(extra['unmapped'][:5])}{'...' if extra['unmapped_count'] > 5 else ''}). "
                                        + ("Load the DETAILED instrument master to get ISINs." if prov == "upstox" else "")})
        for n in extra.get("notes", []):
            warnings.append({"level": "info", "type": "note", "message": n})
        if extra.get("clamped_from"):
            warnings.append({"level": "warning", "type": "provider_range",
                             "message": f"{caps.label} serves {tf.name} candles only from {caps.timeframes[tf.name].earliest_date}; the range "
                                        f"starting {extra['clamped_from']} was shortened. Earlier dates are NOT requested from this provider: "
                                        "choose another provider for them explicitly."})
        if tf.is_intraday and spec.kind == "download" and stored_pin is None and not spec.start_date:
            warnings.append({"level": "info", "type": "history_pin",
                             "message": f"The first real 15-minute run pins the history start at {extra.get('resolved_start')} (five years before that run)."})
        facts = caps.timeframes[tf.name]
        if facts.status != "probe_verified":
            warnings.append({"level": "info", "type": "unverified_limits",
                             "message": f"{caps.label}'s range cap / first date for {tf.name} candles are the provider's published values and have "
                                        "not been verified from this installation (Providers -> Run capability probe)."})
        if caps.adjustment_basis in ("unknown", "adjusted") and prov != "dhan":
            warnings.append({"level": "info", "type": "adjustment_basis",
                             "message": f"{caps.label}'s corporate-action adjustment basis is {caps.adjustment_basis} (unverified). Mixing providers "
                                        "with different bases creates discontinuities; disagreements are recorded, never overwritten."})
        out = {
            "kind": spec.kind, "lane": lane, "provider": prov, "provider_label": caps.label, "timeframe": spec.timeframe,
            "instruments": instruments, "instruments_with_work": with_work, "windows": requests,
            "skipped_failed": extra.get("skipped_failed", 0),
            "resolved_start": extra.get("resolved_start"), "end_exclusive": extra.get("end_exclusive"),
            "end_semantics": ("exclusive: the last candle requested is the day before this date; today's candles are included "
                              "only after the 18:00 IST finalisation boundary"),
            "window_days": extra.get("window_days"),
            "window_note": ("one request per instrument covers the whole history" if spec.timeframe == "daily" and prov == "dhan" and spec.kind == "download"
                            else f"windows of at most {extra.get('window_days')} days ({caps.label}'s own range cap for {tf.name} candles)"),
            "estimated_seconds": int(requests / sustained) if sustained else None,
            "estimate_note": (f"{requests:,} requests at {sustained:g} requests/second ({caps.label}'s binding limit), before retries and "
                              "network latency; real runs take longer"),
            "rps": sustained,
            "budget": {"provider": prov, "max_per_run": lim["per_run"], "max_per_day": lim["per_day"], "used_today": usage["used_today"],
                       "remaining_day": usage["remaining_day"], "live_reserve": reserve, "fits": requests <= room, "room_now": room,
                       "resets_at_ist": usage["resets_at_ist"], "caveat": counter_caveat(prov, caps.label)},
            "disk": {"free_bytes": free, "estimated_new_bytes": est_bytes, "basis": f"{rows_basis}; {bytes_per_row} bytes/row from {basis}; x1.3 headroom",
                     "rows_upper_bound": rows_upper},
            "paths": {"data_dir": str(self.s.data_dir), "database": str(path), "ledger": usage["ledger_path"]},
            "warnings": warnings, "blockers": [w for w in warnings if w["level"] == "blocker"],
            "sample": extra.get("sample", []),
            "command": J.command_text(self._engine_args(spec, ids)),
        }
        for k in ("update", "per_cycle", "cycles", "poll_seconds", "needs_backfill"):
            if k in extra:
                out[k] = extra[k]
        return out

    # ------------------------------------------------------------------ bulk and recovery operations
    def resume_eligible(self) -> list[dict]:
        """Queue every paused / interrupted / stopped / failed job again (the engine skips completed windows)."""
        with self._lock:
            ids = [j["id"] for j in self.store.all(f"SELECT id FROM jobs WHERE status IN ({','.join('?' * len(RESUMABLE))}) ORDER BY id", RESUMABLE)]
            for jid in ids:
                self.resume(jid)
            self.store.audit("queue.resume_eligible", None, {"jobs": ids})
            return [self.job_view(self.get(i)) for i in ids]

    def pause_all(self, *, actor: str = "ui") -> list[dict]:
        """Ask every active worker to pause at its next checkpoint boundary (graceful; nothing is killed)."""
        with self._lock:
            out = []
            for j in self._active_jobs():
                out.append(self.job_view(self.pause(j["id"], actor=actor) if j["status"] != "stopping" else j))
            self.store.audit("queue.pause_all", None, {"jobs": [o["id"] for o in out], "actor": actor})
            return out

    def reconcile(self) -> dict:
        """Re-check every worker now (what the scheduler does every two seconds) and report what changed."""
        with self._lock:
            before = {j["id"]: j["status"] for j in self._active_jobs()}
            self.tick()
            changed = {}
            for jid, old in before.items():
                new = self.get(jid)["status"]
                if new != old:
                    changed[jid] = {"from": old, "to": new}
            self.store.audit("system.reconcile", None, {"changed": changed})
            return {"checked": len(before), "changed": changed, "active": [self.job_view(j) for j in self._active_jobs()]}

    def orphans(self) -> list[dict]:
        """Download worker processes of THIS data directory that no active job accounts for."""
        tracked = set()
        for j in self._active_jobs():
            run = self._latest_run(j["id"])
            if run and run["pid"]:
                tracked.add(run["pid"])
        marker = str(self.s.jobs_dir)
        found = []
        for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
            try:
                cmd = p.info["cmdline"] or []
                if "stockdata.web.worker" in " ".join(cmd) and any(marker in part for part in cmd) and p.info["pid"] not in tracked:
                    found.append({"pid": p.info["pid"], "started": datetime.fromtimestamp(p.info["create_time"], timezone.utc).isoformat(timespec="seconds"),
                                  "control_dir": next((c for c in cmd if marker in c), None)})
            except psutil.Error:
                continue
        return found

    def kill_orphan(self, pid: int) -> dict:
        with self._lock:
            match = next((o for o in self.orphans() if o["pid"] == pid), None)
            if match is None:
                raise JobError(f"Process {pid} is not an untracked download worker of this data directory.", 404)
            outcome = hard_kill(pid, None)
            self.store.audit("system.kill_orphan", f"pid:{pid}", {"outcome": outcome, "control_dir": match["control_dir"]})
            return {"pid": pid, "outcome": outcome}

    # ------------------------------------------------------------------ campaigns
    def create_campaign(self, name: str, specs: list[J.JobSpec], *, continue_on_warnings: bool = False,
                        start_after: datetime | None = None) -> dict:
        with self._lock:
            if not specs:
                raise JobError("a campaign needs at least one job", 400)
            cur = self.store.execute("INSERT INTO campaigns (name, created_at, continue_on_warnings) VALUES (?, ?, ?)",
                                     (name, now(), int(continue_on_warnings)))
            cid = cur.lastrowid
            created = []
            try:
                for pos, spec in enumerate(specs, 1):
                    if pos == 1 and start_after is not None and spec.start_after is None:
                        spec = spec.model_copy(update={"start_after": start_after})
                    created.append(self.create_job(spec, campaign_id=cid, campaign_pos=pos))
            except Exception:
                for j in created:
                    self.store.execute("DELETE FROM jobs WHERE id = ?", (j["id"],))
                self.store.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
                raise
            self.store.audit("campaign.create", f"campaign:{cid}", {"name": name, "jobs": [j["id"] for j in created]})
            return self.campaign_view(cid)

    def campaign_view(self, cid: int) -> dict:
        c = self.store.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
        if not c:
            raise JobError(f"campaign {cid} does not exist", 404)
        jobs = [self.job_view(j) for j in self.store.all("SELECT * FROM jobs WHERE campaign_id = ? ORDER BY campaign_pos", (cid,))]
        statuses = [j["status"] for j in jobs]
        ok = FINISHED_OK if c["continue_on_warnings"] else ("completed",)
        if all(s in FINISHED_OK for s in statuses):
            status, action = "completed", "Nothing left to do."
        elif any(s in ACTIVE for s in statuses):
            status, action = "running", "A job is running; the next one starts automatically when it completes."
        else:
            blocked = next((j for j in jobs if j["status"] not in ok and j["status"] not in ("queued",)), None)
            if blocked:
                status = "needs_attention"
                action = f"Job #{blocked['id']} is {blocked['status']}: {blocked['next_action'] or 'review it'}"
            else:
                status, action = "queued", "Waiting for the queue to start the first job."
        done = sum(1 for s in statuses if s in FINISHED_OK)
        allowed = []
        if any(j["status"] in ACTIVE or j["status"] == "queued" for j in jobs):
            allowed.append("pause")
        if any("resume" in j["actions"] for j in jobs):
            allowed.append("resume")
        if any(j["status"] not in ACTIVE and j["status"] not in FINISHED_OK and j["status"] != "cancelled" for j in jobs):
            allowed.append("cancel")
        if all(j["status"] in FINISHED_OK or j["status"] == "cancelled" for j in jobs):
            allowed.append("delete")
        first = jobs[0] if jobs else None
        return {"id": cid, "name": c["name"], "created_at": c["created_at"], "continue_on_warnings": bool(c["continue_on_warnings"]),
                "status": status, "next_action": action, "jobs": jobs, "jobs_done": done, "jobs_total": len(jobs),
                "scheduled_for": first["scheduled_for"] if first else None, "actions": allowed}

    def campaign_action(self, cid: int, action: str, *, actor: str = "ui") -> dict:
        """Campaign-level control built only from the job-level operations (graceful pause, resume, cancel)."""
        with self._lock:
            view = self.campaign_view(cid)
            jobs = self.store.all("SELECT * FROM jobs WHERE campaign_id = ? ORDER BY campaign_pos", (cid,))
            touched = []
            if action == "pause":
                for j in jobs:
                    if j["status"] in ACTIVE and j["status"] != "stopping":
                        self.pause(j["id"], actor=actor); touched.append(j["id"])
                    elif j["status"] == "queued":
                        self.pause(j["id"], actor=actor); touched.append(j["id"])
            elif action == "resume":
                for j in jobs:
                    if j["status"] in RESUMABLE:
                        self.resume(j["id"]); touched.append(j["id"])
            elif action == "cancel":
                if any(j["status"] in ACTIVE for j in jobs):
                    raise JobError("A job of this campaign is running. Pause it first, then cancel the campaign.", 409)
                for j in jobs:
                    if j["status"] == "queued" or (j["status"] == "paused" and j["runs"] == 0):
                        self.cancel(j["id"]); touched.append(j["id"])
                    elif j["status"] in RESUMABLE:
                        self.cancel(j["id"], abandon=True); touched.append(j["id"])
            else:
                raise JobError(f"unknown campaign action {action!r}", 400)
            self.store.audit(f"campaign.{action}", f"campaign:{cid}", {"jobs": touched, "name": view["name"]})
            self.wake()
            return self.campaign_view(cid)

    def delete_campaign(self, cid: int) -> None:
        with self._lock:
            view = self.campaign_view(cid)
            if "delete" not in view["actions"]:
                raise JobError("Only a finished or cancelled campaign can be removed from the history.", 409)
            for j in view["jobs"]:
                self.delete(j["id"])
            self.store.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
            self.store.audit("campaign.delete", f"campaign:{cid}", {"name": view["name"]})

    def list_campaigns(self) -> list[dict]:
        return [self.campaign_view(c["id"]) for c in self.store.all("SELECT id FROM campaigns ORDER BY id DESC")]
