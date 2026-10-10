"""Automatic updates: the schedule configuration, the runner that executes one run, and the status the interface shows.

A run is NOT a second downloader. It creates ordinary ``update`` jobs and waits for them, so the shared request ledger, the
one-writer-per-database rule, checkpoints, retries and recovery are exactly the ones a person using the interface gets:

    1. refresh the instrument master only when it is older than a configured age (or missing);
    2. for each selected timeframe: an incremental update from the last stored candle (never history), with a configurable
       overlap that re-fetches recent days to catch late revisions;
    3. transient failures are retried within configured limits; everything else stops the run safely:
       a rejected or expired credential ('ACTION REQUIRED'), an exhausted request budget, too little disk space, or a failed
       integrity check;
    4. an integrity check per timeframe, then a summary (providers, requests, candles, failures) in the run record.

The same code runs headless from the Windows Task Scheduler (``python -m stockdata.web automation run``) or inside the
running application ("Run now"). Which one is decided by whether an application server already owns the data directory.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sys
import time
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from stockdata import config as engine_config
from stockdata.db import CandleDB
from stockdata.providers import PROVIDER_NAMES, get_provider
from stockdata.reports import integrity_checks
from stockdata.timeframes import TIMEFRAMES, get_timeframe
from stockdata.timeutil import IST
from stockdata.web import jobs as J
from stockdata.web import taskscheduler as TS
from stockdata.web.process import free_bytes, is_alive
from stockdata.web.redact import redact
from stockdata.web.store import ACTIVE, FINISHED_OK, TERMINAL, Store, now as utc_iso
from stockdata.web.supervisor import JobError

log = logging.getLogger("stockdata.automation")

CONFIG_KEY = "automation_config"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# runner exit codes (the Task Scheduler shows them as "last result")
EXIT_OK, EXIT_WARN, EXIT_ACTION, EXIT_BUDGET, EXIT_STOPPED, EXIT_DISK, EXIT_INTEGRITY, EXIT_NO_SERVER, EXIT_BUSY = 0, 2, 3, 4, 5, 6, 7, 8, 75
STATUS_EXIT = {"completed": EXIT_OK, "skipped": EXIT_OK, "completed_with_warnings": EXIT_WARN, "action_required": EXIT_ACTION,
               "stopped_budget": EXIT_BUDGET, "stopped": EXIT_STOPPED, "stopped_disk": EXIT_DISK, "integrity_failed": EXIT_INTEGRITY,
               "busy": EXIT_BUSY}
WAITING = ("queued", "preparing", "running", "stopping")


# ---------------------------------------------------------------------------------------------- configuration
class Universe(BaseModel):
    mode: Literal["all", "group"] = "all"
    group_id: int | None = None

    @model_validator(mode="after")
    def _ok(self):
        if self.mode == "group" and self.group_id is None:
            raise ValueError("choose a saved group")
        return self


class AutomationConfig(BaseModel):
    enabled: bool = False
    provider: Literal["dhan", "fyers", "upstox"] | None = Field(default=None, description="None = the default provider of each timeframe")
    timeframes: list[Literal["daily", "15min"]] = Field(default_factory=lambda: ["daily", "15min"], min_length=1)
    universe: Universe = Field(default_factory=Universe)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    holidays: list[date] = Field(default_factory=list, description="market holidays (IST dates) on which scheduled runs are skipped")
    post_close_time: str = "18:15"
    reconcile_enabled: bool = True
    reconcile_time: str = "08:30"
    overlap_days: int = Field(default=engine_config.DEFAULT_OVERLAP_DAYS, ge=0, le=60)
    reconcile_overlap_days: int = Field(default=14, ge=0, le=60)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_delay_minutes: int = Field(default=10, ge=0, le=120)
    master_refresh_days: int = Field(default=7, ge=1, le=90)
    max_runtime_hours: int = Field(default=6, ge=1, le=24)
    run_whether_logged_on: bool = False
    wake_to_run: bool = False

    @field_validator("post_close_time", "reconcile_time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not TIME_RE.match(v):
            raise ValueError("times are HH:MM, 24-hour, Asia/Kolkata")
        return v

    @field_validator("weekdays")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if not v or any(d not in range(7) for d in v):
            raise ValueError("choose at least one weekday (0 = Monday ... 6 = Sunday)")
        return sorted(set(v))

    @field_validator("timeframes")
    @classmethod
    def _tfs(cls, v: list[str]) -> list[str]:
        return [t for t in ("daily", "15min") if t in v]                 # daily always before 15-minute


def load_config(store: Store) -> AutomationConfig:
    raw = store.get_meta(CONFIG_KEY)
    return AutomationConfig.model_validate(raw) if raw else AutomationConfig()


def save_config(store: Store, cfg: AutomationConfig) -> None:
    store.set_meta(CONFIG_KEY, cfg.model_dump(mode="json"))
    store.audit("automation.config", None, {"enabled": cfg.enabled, "timeframes": cfg.timeframes, "provider": cfg.provider,
                                            "post_close_time": cfg.post_close_time, "reconcile": cfg.reconcile_enabled})


# ------------------------------------------------------------------------------------------------ the runner
@dataclass
class RunResult:
    status: str
    exit_code: int
    summary: dict


def classify_block(reason: str | None) -> str | None:
    """A queued job's block reason -> the run status it implies, or None when waiting is the right thing to do."""
    r = (reason or "").lower()
    if not r:
        return None
    if "credential" in r or "token has expired" in r or "action required" in r or "has expired" in r:
        return "action_required"
    if "budget" in r and "used up" in r:
        return "stopped_budget"
    if "free disk" in r or "not enough free disk" in r:
        return "stopped_disk"
    if "queue processing is switched off" in r:
        return "stopped"
    return None


class AutomationRunner:
    def __init__(self, sup, store: Store, data, gate, settings, *, tick: Callable[[], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], datetime] = lambda: datetime.now(IST),
                 stop: Callable[[], bool] | None = None):
        self.sup, self.store, self.data, self.gate, self.s = sup, store, data, gate, settings
        self.tick = tick or sup.tick
        self.sleep, self.clock = sleep, clock
        self.stop = stop or (lambda: False)

    # ------------------------------------------------------------------------------------ records
    def _start_record(self, trigger: str, phase: str) -> int:
        cur = self.store.execute("INSERT INTO automation_runs (trigger, started_at, status, pid, summary) VALUES (?, ?, 'running', ?, ?)",
                                 (trigger, utc_iso(), os.getpid(), json.dumps({"phase": phase})))
        return cur.lastrowid

    def _finish_record(self, run_id: int, res: RunResult) -> None:
        self.store.execute("UPDATE automation_runs SET finished_at = ?, status = ?, exit_code = ?, summary = ? WHERE id = ?",
                           (utc_iso(), res.status, res.exit_code, json.dumps(res.summary, default=str), run_id))
        self.store.audit("automation.run", f"run:{run_id}", {"status": res.status, "exit_code": res.exit_code})

    def _note(self, run_id: int, **fields) -> None:
        row = self.store.one("SELECT summary FROM automation_runs WHERE id = ?", (run_id,))
        cur = json.loads(row["summary"] or "{}") if row else {}
        cur.update(fields)
        self.store.execute("UPDATE automation_runs SET summary = ? WHERE id = ?", (json.dumps(cur, default=str), run_id))

    # ------------------------------------------------------------------------------------- the run
    def run(self, *, phase: str = "post-close", trigger: str = "manual") -> RunResult:
        cfg = load_config(self.store)
        run_id = self._start_record(trigger, phase)
        summary: dict = {"phase": phase, "trigger": trigger, "timeframes": {}, "jobs": [], "providers": [], "notes": [], "run_id": run_id}
        try:
            res = self._run(cfg, phase, trigger, summary, run_id)
        except Exception as exc:                                  # noqa: BLE001 - a run must always leave a record
            log.exception("automatic run failed")
            summary["notes"].append(f"internal error: {type(exc).__name__}: {redact(str(exc))[:300]}")
            res = RunResult("stopped", EXIT_STOPPED, summary)
        self._finish_record(run_id, res)
        return res

    def _run(self, cfg: AutomationConfig, phase: str, trigger: str, summary: dict, run_id: int) -> RunResult:
        now = self.clock()
        if trigger == "scheduled":
            if not cfg.enabled:
                summary["notes"].append("automatic updates are switched off: nothing to do")
                return RunResult("skipped", EXIT_OK, summary)
            if now.weekday() not in cfg.weekdays or now.date() in cfg.holidays:
                summary["notes"].append(f"{now:%Y-%m-%d} is not a market day in the configuration (weekday or listed holiday): skipped")
                return RunResult("skipped", EXIT_OK, summary)
        if phase == "reconcile" and not cfg.reconcile_enabled and trigger == "scheduled":
            summary["notes"].append("the reconciliation run is switched off: skipped")
            return RunResult("skipped", EXIT_OK, summary)
        eff = self.sup.effective()
        if free_bytes(self.s.data_dir) < int(eff["min_free_gb"] * 1024 ** 3) // 2:
            summary["notes"].append("not enough free disk space: no download was started")
            return RunResult("stopped_disk", EXIT_DISK, summary)
        deadline = time.monotonic() + cfg.max_runtime_hours * 3600
        overlap = cfg.reconcile_overlap_days if phase == "reconcile" else cfg.overlap_days
        providers: list[str] = []
        worst = "completed"
        # 1. instrument metadata, only when it is needed
        status = self._maybe_refresh_master(cfg, summary, deadline)
        if status:
            return self._result(status, summary)
        # 2. every timeframe, daily first
        for tf_name in cfg.timeframes:
            prov = cfg.provider or self.sup.default_provider(tf_name)
            if prov not in providers:
                providers.append(prov)
            summary["providers"] = providers
            problem = self.sup.provider_ready(prov)
            if problem:
                summary["timeframes"][tf_name] = {"status": "action_required", "provider": prov, "message": problem}
                return self._result("action_required", summary)
            tf_status = self._update_timeframe(cfg, tf_name, prov, overlap, phase == "reconcile", summary, deadline)
            self._note(run_id, **{k: v for k, v in summary.items() if k != "run_id"})
            if tf_status in ("action_required", "stopped_budget", "stopped_disk", "stopped", "integrity_failed"):
                return self._result(tf_status, summary)
            if tf_status == "completed_with_warnings":
                worst = tf_status
        return self._result(worst, summary)

    def _result(self, status: str, summary: dict) -> RunResult:
        summary["rows_inserted"] = sum(j.get("rows_inserted") or 0 for j in summary["jobs"])
        by_provider: dict[str, int] = {}
        for j in summary["jobs"]:
            by_provider[j["provider"]] = by_provider.get(j["provider"], 0) + (j.get("attempts_used") or 0)
        summary["requests"] = by_provider
        return RunResult(status, STATUS_EXIT[status], summary)

    # ------------------------------------------------------------------------------------- master data
    def _maybe_refresh_master(self, cfg: AutomationConfig, summary: dict, deadline: float) -> str | None:
        age = self._master_age_days()
        need = age is None or age > cfg.master_refresh_days
        summary["master"] = {"age_days": age, "refresh_needed": need}
        if not need:
            return None
        provs = {cfg.provider or self.sup.default_provider(t) for t in cfg.timeframes}
        kind = "detailed" if "upstox" in provs else "compact"
        spec = J.JobSpec(kind="instruments_load", master_kind=kind)
        job = self.sup.create_job(spec, allow_duplicate=True, actor="automation")
        final = self._await([job["id"]], deadline)           # the queue (and its on/off switch) decides when it starts
        if final != "done":
            self._withdraw(job["id"])
        j = self.sup.get(job["id"])
        summary["master"].update(job_id=job["id"], status=j["status"], kind=kind)
        if j["status"] in FINISHED_OK:
            return None
        if final != "done":
            return final
        summary["notes"].append("the instrument master could not be refreshed; the run continues with the one already loaded")
        return None

    def _master_age_days(self) -> float | None:
        for tf in ("daily", "15min"):
            path = self.data.path(tf)
            if not path.exists():
                continue
            try:
                with self.gate.reader(tf):
                    with CandleDB(get_timeframe(tf), path, read_only=True) as db:
                        row = db.con.execute("SELECT max(loaded_at_utc) FROM instrument_master_loads").fetchone()
                if row and row[0]:
                    return (datetime.now(timezone.utc).replace(tzinfo=None) - row[0]).total_seconds() / 86400
            except Exception:                                     # noqa: BLE001 - busy or unreadable: treat as unknown, do not guess
                continue
        return None

    # ----------------------------------------------------------------------------------- one timeframe
    def _selection(self, cfg: AutomationConfig) -> J.Selection:
        if cfg.universe.mode == "group":
            return J.Selection(mode="group", group_id=cfg.universe.group_id)
        return J.Selection(mode="all")

    def _ensure_job(self, spec: J.JobSpec) -> dict:
        """Create the job, or adopt an identical one that is already waiting/running (a retried or overlapping trigger)."""
        try:
            return self.sup.create_job(spec, actor="automation")
        except JobError as exc:
            if exc.status != 409 or "identical job already exists" not in str(exc):
                raise
            ids = self.sup._resolve_ids(spec)
            prov = spec.provider or self.sup.default_provider(spec.timeframe)
            fp = J.fingerprint(spec.model_copy(update={"provider": prov}), ids)
            dup = self.store.one("SELECT * FROM jobs WHERE fingerprint = ? AND status NOT IN ('completed','completed_with_warnings','cancelled') ORDER BY id DESC", (fp,))
            if not dup:
                raise
            return dup

    def _update_timeframe(self, cfg, tf_name, prov, overlap, reconcile, summary, deadline) -> str:
        rec = summary["timeframes"].setdefault(tf_name, {"provider": prov, "jobs": [], "rounds": 0})
        spec = J.JobSpec(kind="update", timeframe=tf_name, provider=prov, selection=self._selection(cfg), overlap_days=overlap,
                         retry_failed=reconcile)
        job = self._ensure_job(spec)
        rounds = 0
        while True:
            outcome = self._await([job["id"]], deadline)
            j = self.sup.get(job["id"])
            view = self.sup.job_view(j)
            summary["jobs"].append({"id": j["id"], "name": j["name"], "timeframe": tf_name, "provider": prov, "status": j["status"],
                                    "rows_inserted": j["rows_inserted"], "attempts_used": j["attempts_used"],
                                    "failed_windows": j["failed_windows"], "retriable_windows": j["retriable_windows"],
                                    "next_action": view["next_action"]})
            rec["jobs"].append(j["id"])
            rec["rounds"] = rounds + 1
            if outcome != "done":
                rec["status"] = outcome
                self._withdraw(job["id"])
                return outcome
            if j["status"] == "completed":
                rec["status"] = "completed"
                break
            if j["status"] == "completed_with_warnings":
                if rounds >= cfg.max_retries:
                    rec["status"] = "completed_with_warnings"
                    summary["notes"].append(f"{tf_name}: {j['failed_windows'] + j['retriable_windows']} window(s) still failing after "
                                            f"{rounds} retr{'y' if rounds == 1 else 'ies'}; they are listed in Data Quality and can be retried")
                    break
                rounds += 1
                self._pause_before_retry(cfg.retry_delay_minutes, deadline)
                if self.stop():
                    rec["status"] = "stopped"
                    return "stopped"
                job = self.sup.retry_failed(j["id"])
                continue
            if j["status"] == "stopped_by_limit":
                rec["status"] = "stopped_budget"
                return "stopped_budget"
            if j["status"] == "failed":
                text = ((j["last_error"] or "") + " " + (j["next_action"] or "")).lower()
                rec["status"] = "action_required" if ("token" in text or "authentication" in text or "credential" in text) else "stopped"
                rec["message"] = redact(j["last_error"] or j["next_action"] or "the update failed")[:300]
                return rec["status"]
            rec["status"] = "stopped"
            return "stopped"
        # integrity check before anything else is allowed to run on top of this database
        bad = self._integrity(tf_name)
        rec["integrity"] = bad
        if bad:
            rec["status"] = "integrity_failed"
            summary["notes"].append(f"{tf_name}: {len(bad)} integrity check(s) failed: {'; '.join(b['name'] for b in bad[:3])}")
            return "integrity_failed"
        return rec["status"]

    # ---------------------------------------------------------------------------------------- waiting
    def _await(self, job_ids: list[int], deadline: float) -> str:
        """'done' when every job left the working states; otherwise the run status that stopped the wait."""
        blocked_since: dict[int, float] = {}
        while True:
            if self.stop():
                return "stopped"
            if time.monotonic() > deadline:
                return "stopped"
            self.tick()
            jobs = [self.sup.get(i) for i in job_ids]
            if all(j["status"] not in WAITING for j in jobs):
                return "done"
            for j in jobs:
                if j["status"] == "queued":
                    implied = classify_block(j.get("block_reason"))
                    if implied:
                        blocked_since.setdefault(j["id"], time.monotonic())
                        if time.monotonic() - blocked_since[j["id"]] >= 2:
                            return implied
                    else:
                        blocked_since.pop(j["id"], None)
            self.sleep(1.0)

    def _withdraw(self, job_id: int) -> None:
        """A job this run created but could not start is removed from the queue, so blocked runs never pile up a backlog."""
        try:
            if self.sup.get(job_id)["status"] == "queued":
                self.sup.cancel(job_id)
        except JobError:
            pass

    def _pause_before_retry(self, minutes: int, deadline: float) -> None:
        end = time.monotonic() + minutes * 60
        while time.monotonic() < min(end, deadline) and not self.stop():
            self.tick()
            self.sleep(min(5.0, max(0.0, end - time.monotonic())))

    def _integrity(self, tf_name: str) -> list[dict]:
        tf = get_timeframe(tf_name)
        path = self.data.path(tf_name)
        if not path.exists():
            return []
        try:
            with self.gate.reader(tf_name):
                with CandleDB(tf, path, read_only=True) as db:
                    return [{"name": c.name, "detail": c.detail} for c in integrity_checks(db) if not c.ok]
        except Exception as exc:                                  # noqa: BLE001 - busy: report as not checked rather than failing the run
            log.warning("integrity check skipped (%s): %s", tf_name, exc)
            return []


# ---------------------------------------------------------------------------------------------- the service
class AutomationService:
    """Configuration, Task Scheduler management and status for the Automation screen."""

    def __init__(self, settings, store: Store, sup, data, gate, *, backend: TS.SchedulerBackend | None = None):
        self.s, self.store, self.sup, self.data, self.gate = settings, store, sup, data, gate
        self.backend = backend or TS.get_backend()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------------------ task specs
    def _python(self) -> str:
        venv = self.s.repo_root / ".venv" / "Scripts"
        for name in ("pythonw.exe", "python.exe"):                 # pythonw: no console window flashes up on the desktop
            if (venv / name).exists():
                return str(venv / name)
        return sys.executable

    def specs(self, cfg: AutomationConfig) -> list[TS.TaskSpec]:
        base = dict(weekdays=cfg.weekdays, python=self._python(), repo=str(self.s.repo_root), data_dir=str(self.s.data_dir),
                    run_whether_logged_on=cfg.run_whether_logged_on, wake_to_run=cfg.wake_to_run,
                    execution_limit_hours=cfg.max_runtime_hours + 1)
        out = [TS.TaskSpec(phase="post-close", time_ist=cfg.post_close_time, **base)]
        if cfg.reconcile_enabled:
            out.append(TS.TaskSpec(phase="reconcile", time_ist=cfg.reconcile_time, **base))
        return out

    def plan(self) -> dict:
        """What a run would do right now, from the real planners (nothing is requested)."""
        cfg = load_config(self.store)
        out = {"timeframes": [], "master": {"age_days": None}}
        for tf_name in cfg.timeframes:
            prov = cfg.provider or self.sup.default_provider(tf_name)
            spec = J.JobSpec(kind="update", timeframe=tf_name, provider=prov,
                             selection=J.Selection(mode="group", group_id=cfg.universe.group_id) if cfg.universe.mode == "group" else J.Selection(mode="all"),
                             overlap_days=cfg.overlap_days)
            try:
                p = self.sup.preview(spec)
                out["timeframes"].append({"timeframe": tf_name, "provider": prov, "label": p["provider_label"], "requests": p["windows"],
                                          "update": p.get("update"), "needs_backfill": p.get("needs_backfill", []),
                                          "warnings": [w for w in p["warnings"] if w["level"] in ("blocker", "warning")],
                                          "estimated_seconds": p["estimated_seconds"], "budget": p["budget"]})
            except Exception as exc:                              # noqa: BLE001 - a database being written is shown, not fatal
                out["timeframes"].append({"timeframe": tf_name, "provider": prov, "error": redact(str(exc))[:300]})
        return out

    # ---------------------------------------------------------------------------------- management
    def install(self) -> dict:
        cfg = load_config(self.store)
        installed: list[str] = []
        notice: str | None = None
        specs = self.specs(cfg)
        i = 0
        while i < len(specs):
            spec = specs[i]
            try:
                self.backend.register(TS.task_path(spec.phase), TS.build_task_xml(spec))
            except TS.SchedulerError as exc:
                if spec.run_whether_logged_on and notice is None and self._interactive_task_registers(spec):
                    # Windows lets only an administrator (or an account holding the "Log on as a batch job" right) create a task
                    # that runs while nobody is signed in. Fall back to the task that runs while YOU are signed in, say so, and
                    # remember it so the saved settings and the installed task agree.
                    cfg = cfg.model_copy(update={"run_whether_logged_on": False})
                    save_config(self.store, cfg)
                    notice = TS.LOGGED_OFF_REFUSED.replace("{detail}", str(exc))
                    specs, installed, i = self.specs(cfg), [], 0
                    continue
                raise JobError(TS.explain_register_failure(str(exc)), 400)
            installed.append(spec.phase)
            i += 1
        if not cfg.reconcile_enabled:                              # a switched-off reconciliation must not linger as a task
            try:
                if self.backend.available and self.backend.info(TS.task_path("reconcile")):
                    self.backend.unregister(TS.task_path("reconcile"))
            except TS.SchedulerError:
                pass
        self.store.audit("automation.task.install", None, {"tasks": installed, "signed_in_only_fallback": notice is not None})
        return {**self.status(), "notice": notice}

    def _interactive_task_registers(self, spec: TS.TaskSpec) -> bool:
        """Would the same task, running only while the user is signed in, be accepted? (Registers it if so.)"""
        try:
            self.backend.register(TS.task_path(spec.phase), TS.build_task_xml(dataclasses.replace(spec, run_whether_logged_on=False)))
            return True
        except TS.SchedulerError:
            return False

    def remove(self) -> dict:
        for phase in TS.TASKS:
            try:
                if self.backend.info(TS.task_path(phase)):
                    self.backend.unregister(TS.task_path(phase))
            except TS.SchedulerError as exc:
                raise JobError(str(exc), 400)
        self.store.audit("automation.task.remove")
        return self.status()

    def set_enabled(self, enabled: bool) -> dict:
        for phase in TS.TASKS:
            try:
                if self.backend.info(TS.task_path(phase)):
                    self.backend.set_enabled(TS.task_path(phase), enabled)
            except TS.SchedulerError as exc:
                raise JobError(str(exc), 400)
        self.store.audit("automation.task.enable" if enabled else "automation.task.disable")
        return self.status()

    def xml_preview(self) -> list[dict]:
        cfg = load_config(self.store)
        return [{"phase": s.phase, "task": TS.task_path(s.phase), "arguments": TS.arguments_for(s), "xml": TS.build_task_xml(s)}
                for s in self.specs(cfg)]

    # --------------------------------------------------------------------------------------- running
    def run_now(self, phase: str = "post-close") -> dict:
        """'Run now' inside the application: the same runner, in a background thread, using the application's scheduler."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise JobError("an automatic run is already in progress", 409)
            if self._running_record():
                raise JobError("an automatic run is already in progress (started by the Task Scheduler)", 409)
            runner = AutomationRunner(self.sup, self.store, self.data, self.gate, self.s, tick=lambda: None)
            holder: dict = {}

            def work():
                holder["result"] = runner.run(phase=phase, trigger="manual")

            self._thread = threading.Thread(target=work, name="stockdata-automation", daemon=True)
            self._thread.start()
        for _ in range(50):                                       # the record exists as soon as the runner starts
            rec = self._running_record()
            if rec:
                return {"started": True, "run_id": rec["id"]}
            time.sleep(0.05)
        return {"started": True, "run_id": None}

    def _running_record(self) -> dict | None:
        self._reap_dead_runs()
        return self.store.one("SELECT * FROM automation_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1")

    def _reap_dead_runs(self) -> None:
        """A run whose process vanished (power loss, kill) is recorded as abandoned; the next run simply continues."""
        for r in self.store.all("SELECT * FROM automation_runs WHERE status = 'running'"):
            if r["pid"] == os.getpid() and self._thread and self._thread.is_alive():
                continue
            if r["pid"] and is_alive(r["pid"], None) and r["pid"] != os.getpid():
                continue
            self.store.execute("UPDATE automation_runs SET status = 'abandoned', finished_at = ? WHERE id = ?", (utc_iso(), r["id"]))

    # ---------------------------------------------------------------------------------------- status
    def runs(self, limit: int = 20) -> list[dict]:
        self._reap_dead_runs()
        out = []
        for r in self.store.all("SELECT * FROM automation_runs ORDER BY id DESC LIMIT ?", (limit,)):
            s = json.loads(r["summary"] or "{}")
            out.append({"id": r["id"], "trigger": r["trigger"], "phase": s.get("phase"), "started_at": r["started_at"],
                        "finished_at": r["finished_at"], "status": r["status"], "exit_code": r["exit_code"],
                        "exit_text": TS.RUNNER_EXIT.get(r["exit_code"]) if r["exit_code"] is not None else None, "summary": s})
        return out

    def missed(self, cfg: AutomationConfig, runs: list[dict], *, now: datetime | None = None, days: int = 14) -> list[dict]:
        """Market days (IST) whose post-close run never started, by the recorded runs. Informational: an update is incremental,
        so the next run that does start catches the missed days up by itself."""
        now = (now or datetime.now(IST)).astimezone(IST)
        if not cfg.enabled:
            return []
        hh, mm = (int(x) for x in cfg.post_close_time.split(":"))
        have = set()
        for r in runs:
            if r.get("phase") == "post-close" and r["status"] not in ("busy",) and r["started_at"]:
                started = datetime.fromisoformat(r["started_at"]).astimezone(IST)
                have.add(started.date())
                if started.hour < hh:                                 # a catch-up shortly after midnight covers the previous day
                    have.add(started.date() - timedelta(days=1))
        out = []
        for back in range(1, days + 1):
            d = (now - timedelta(days=back)).date()
            due = datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)
            if d.weekday() in cfg.weekdays and d not in cfg.holidays and due < now and d not in have:
                out.append({"date": d.isoformat(), "was_due": due.isoformat(timespec="minutes")})
        return out

    def status(self) -> dict:
        cfg = load_config(self.store)
        runs = self.runs()
        specs = {s.phase: s for s in self.specs(cfg)}
        tasks = []
        for phase in TS.TASKS:
            spec = specs.get(phase) or TS.TaskSpec(phase=phase, time_ist=cfg.reconcile_time if phase == "reconcile" else cfg.post_close_time,
                                                    weekdays=cfg.weekdays, python=self._python(), repo=str(self.s.repo_root),
                                                    data_dir=str(self.s.data_dir))
            expected = TS.build_task_xml(spec) if phase in specs else None
            st = TS.task_status(self.backend, spec, expected_xml=expected)
            st["wanted"] = phase in specs
            st["time_ist"] = spec.time_ist
            st["next_runs_ist"] = TS.next_runs(spec.time_ist, cfg.weekdays, count=3) if phase in specs else []
            tasks.append(st)
        last_ok = next((r for r in runs if r["status"] in ("completed", "completed_with_warnings") and r["trigger"] in ("scheduled", "manual")), None)
        last_scheduled = next((r for r in runs if r["trigger"] == "scheduled"), None)
        errors = [r for r in runs if r["status"] in ("action_required", "stopped_budget", "stopped_disk", "integrity_failed", "stopped")][:5]
        providers = sorted({p for r in runs[:5] for p in (r["summary"].get("providers") or [])}) or [cfg.provider or self.sup.default_provider("daily")]
        return {"config": cfg.model_dump(mode="json"), "scheduler": {"backend": self.backend.name, "available": self.backend.available},
                "tasks": tasks, "runs": runs, "last_success": last_ok, "last_scheduled_run": last_scheduled,
                "missed_executions": self.missed(cfg, runs), "recent_problems": errors, "running": self._running_record(),
                "recovery": self._recovery_actions(runs, cfg, providers),
                "unattended_note": ("Unattended updates are possible only while the provider credential stays valid. "
                                    + ("FYERS needs a person to log in to renew its token. " if "fyers" in providers else "")
                                    + "A scheduled run that finds an expired credential stops with ACTION REQUIRED instead of retrying blindly.")}

    def _recovery_actions(self, runs: list[dict], cfg: AutomationConfig, providers: list[str]) -> list[dict]:
        out = []
        for r in runs[:3]:
            s = r["status"]
            if s == "action_required":
                out.append({"problem": "A credential was rejected or has expired",
                            "action": "Renew the token on the Providers screen, test the connection, then press 'Run now' (or wait for the next scheduled run)."})
            elif s == "stopped_budget":
                out.append({"problem": "The daily request budget was reached",
                            "action": "Nothing is lost: the next run continues from the last stored candle. Raise the provider's limit only if its terms allow."})
            elif s == "stopped_disk":
                out.append({"problem": "Free disk space is too low", "action": "Free space on the data drive, then press 'Run now'."})
            elif s == "integrity_failed":
                out.append({"problem": "An integrity check failed after an update",
                            "action": "Open Data Quality for the timeframe named in the run, review the failed check, and use a targeted repair if needed."})
            elif s == "abandoned":
                out.append({"problem": "A run was interrupted (the computer stopped or the process ended)",
                            "action": "No action is needed: unfinished work resumes from its checkpoints on the next run."})
        seen, uniq = set(), []
        for a in out:
            if a["problem"] not in seen:
                seen.add(a["problem"])
                uniq.append(a)
        return uniq
