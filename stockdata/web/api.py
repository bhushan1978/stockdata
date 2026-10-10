"""The HTTP API (FastAPI) and the single-page application host.

Local only: the server binds to loopback, refuses requests whose Host header is not loopback, and refuses state-changing
requests that do not come from this application (custom header + Origin check), which defeats cross-site and
DNS-rebinding requests from other web pages. No endpoint runs arbitrary commands, arbitrary SQL, or reads arbitrary
files; every spend-requests or alter-data action is explicit and written to the audit log.
Interactive documentation of all request/response models: ``/api/docs``.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
import shutil
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from stockdata.timeframes import TIMEFRAMES
from stockdata.web import jobs as J
from stockdata.web.credentials import CredentialStore
from stockdata.web.data import DataAccess, DatabaseBusy, DatabaseMissing, DbGate
from stockdata.web.maintenance import Maintenance
from stockdata.web.system import SystemInfo
from stockdata.web.quality import QualityRunner, coverage_csv
from stockdata.web.redact import redact
from stockdata.web.settings import BOOTSTRAP_FILE, WebSettings, read_bootstrap, write_bootstrap
from stockdata.web.store import ACTIVE, Store
from stockdata.web.supervisor import JobError, Supervisor

log = logging.getLogger("stockdata.web")
Timeframe = Literal["daily", "15min"]
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


# ------------------------------------------------------------------------------------------------ models
class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class JobOut(Loose):
    id: int
    name: str
    kind: str
    timeframe: str | None = None
    status: str
    command: str | None = None
    progress: dict | None = None
    worker: dict | None = None
    actions: list[str] = []
    next_action: str | None = None
    note: str | None = None
    last_error: str | None = None


class PlanOut(Loose):
    timeframe: str
    instruments: int
    windows: int
    resolved_start: str
    end_exclusive: str
    warnings: list[dict] = []
    blockers: list[dict] = []


class CreateJobIn(BaseModel):
    spec: J.JobSpec
    start: Literal["queue", "now"] = "queue"
    confirm: bool = Field(False, description="must be true: creating a job is an explicit action")


class JobActionIn(BaseModel):
    confirm: bool = False
    security_ids: list[int] | None = None
    empty_error_codes: list[str] | None = None
    confirm_full: bool = False


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    jobs: list[J.JobSpec]
    continue_on_warnings: bool = False
    start_after: datetime | None = Field(default=None, description="the first job does not start before this time (naive = IST)")
    confirm: bool = False


class SettingsIn(BaseModel):
    max_per_run: int | None = Field(None, ge=1)
    max_per_day: int | None = Field(None, ge=1)
    rps: float | None = Field(None, gt=0, le=5)
    parallel_timeframes: bool | None = None
    queue_enabled: bool | None = None
    auto_resume_budget: bool | None = None
    min_free_gb: float | None = Field(None, ge=0, le=100_000)


class CredentialsIn(BaseModel):
    access_token: str | None = Field(None, repr=False)
    client_id: str | None = Field(None, repr=False)


class DataDirIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    confirm: bool = False
    confirm_new_empty: bool = False


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    security_ids: list[int] = Field(min_length=1, max_length=J.MAX_EXPLICIT_INSTRUMENTS)


class ConfirmIn(BaseModel):
    confirm: bool = False


class ShutdownIn(BaseModel):
    stop_jobs: bool = False
    confirm: bool = False


# ------------------------------------------------------------------------------------------------ app
def create_app(settings: WebSettings, *, start_scheduler: bool = True) -> FastAPI:
    started = time.time()
    store = Store(settings.control_db)
    creds = CredentialStore(settings.credentials_file)
    holder: dict[str, Any] = {}
    gate = DbGate(lambda tf: holder["sup"].writer_for(tf))
    data = DataAccess(settings.data_dir, store, gate)
    sup = Supervisor(settings, store, creds, data, gate)
    holder["sup"] = sup
    quality = QualityRunner(data, store)
    maint = Maintenance(settings.data_dir, data, store)
    sysinfo = SystemInfo(settings, sup, data, creds)
    if start_scheduler:
        sup.start()
    else:
        sup.recover()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if sup._thread:                       # stop the scheduler; detached download workers are NOT touched
            sup.shutdown()

    app = FastAPI(title="Stock Data Management Center", version=settings.version, docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.sup, app.state.store, app.state.data, app.state.creds, app.state.settings = sup, store, data, creds, settings
    app.state.server = None            # set by __main__ (uvicorn server) so shutdown can stop it

    hosts = LOOPBACK | set(settings.extra_hosts)
    origins = {f"http://127.0.0.1:{settings.port}", f"http://localhost:{settings.port}", *settings.dev_origins}
    origins |= {f"http://{h}" for h in settings.extra_hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        name = host.rsplit(":", 1)[0].strip("[]") if not host.startswith("[") else host.split("]")[0].strip("[")
        if name not in hosts:
            return JSONResponse({"error": "This application only answers on localhost."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":      # another website's page, even for a plain GET
            return JSONResponse({"error": "Request refused: cross-site requests are not allowed."}, status_code=403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if (origin and origin not in origins) or request.headers.get("x-stockdata-ui") != "1":
                return JSONResponse({"error": "Request refused: it did not come from this application."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                       "connect-src 'self'; frame-ancestors 'none'")
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ---- errors: never leak a secret or a stack trace
    @app.exception_handler(JobError)
    async def _job_error(_, exc: JobError):
        return JSONResponse({"error": redact(str(exc))}, status_code=exc.status)

    @app.exception_handler(DatabaseBusy)
    async def _busy(_, exc: DatabaseBusy):
        return JSONResponse({"error": str(exc), "busy": True, "timeframe": exc.timeframe, "job_id": exc.job_id,
                             "external": exc.external}, status_code=423)

    @app.exception_handler(DatabaseMissing)
    async def _missing(_, exc: DatabaseMissing):
        return JSONResponse({"error": str(exc), "missing": True}, status_code=404)

    @app.exception_handler(KeyError)
    async def _key(_, exc: KeyError):
        return JSONResponse({"error": redact(str(exc.args[0]) if exc.args else "not found")}, status_code=404)

    @app.exception_handler(ValueError)
    async def _value(_, exc: ValueError):
        return JSONResponse({"error": redact(str(exc))}, status_code=400)

    @app.exception_handler(Exception)
    async def _any(_, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse({"error": "Internal error: " + redact(f"{type(exc).__name__}: {exc}")[:300]}, status_code=500)

    def need_confirm(ok: bool, what: str) -> None:
        if not ok:
            raise JobError(f"{what} needs explicit confirmation (confirm: true).", 400)

    # ============================================================================ system
    def disk() -> dict:
        probe = settings.data_dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        u = shutil.disk_usage(probe)
        eff = sup.effective()
        warn = eff["min_free_gb"] * 1024 ** 3
        level = "critical" if u.free < warn / 2 else "warning" if u.free < warn else "ok"
        return {"path": str(probe), "free_bytes": u.free, "total_bytes": u.total, "level": level, "warn_below_gb": eff["min_free_gb"]}

    def health() -> dict:
        t = sup._thread
        active = sup._active_jobs()
        return {"status": "ok", "version": settings.version, "commit": settings.commit, "pid": os.getpid(),
                "uptime_seconds": int(time.time() - started), "scheduler_alive": bool(t and t.is_alive()),
                "queue_enabled": sup.effective()["queue_enabled"], "workers_active": len(active),
                "server_time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "bind": f"{settings.host}:{settings.port}", "control_db_ok": store.one("SELECT 1 AS ok")["ok"] == 1}

    @app.get("/api/health", tags=["system"])
    def api_health() -> dict:
        return health()

    @app.get("/api/usage", tags=["system"])
    def api_usage() -> dict:
        return sup.usage()

    @app.get("/api/overview", tags=["system"])
    def api_overview() -> dict:
        summaries = {tf: data.summary(tf) for tf in TIMEFRAMES}
        counts = sup.counts()
        jobs = sup.list_jobs()
        unfinished = [j for j in jobs if j["status"] not in ("completed", "completed_with_warnings", "cancelled")]
        pending = sum(max(0, (j["windows_planned"] or 0) - (j["windows_done"] or 0)) for j in unfinished if j["windows_planned"])
        resumable = [j for j in jobs if "resume" in j["actions"]]
        master = next((s["master_load"] for s in summaries.values() if s.get("master_load")), None)
        last_ok = [j for j in jobs if j["status"] in ("completed", "completed_with_warnings") and j["kind"] == "download"]
        last_ok.sort(key=lambda j: j["finished_at"] or "", reverse=True)
        outstanding = sum(s.get("failed_windows_recorded") or 0 for s in summaries.values())
        return {"health": health(), "databases": summaries, "jobs": counts, "usage": sup.usage(), "disk": disk(),
                "master": master, "pending_windows": pending if unfinished else 0,
                "pending_note": "windows still to request in unfinished jobs (from their own plans); not an estimate of the whole universe",
                "outstanding_failed_windows": outstanding, "active": [j for j in jobs if j["status"] in ACTIVE],
                "resumable": resumable, "last_successful_job": last_ok[0] if last_ok else None,
                "credentials": creds.status(), "paths": {"data_dir": str(settings.data_dir)}}

    @app.post("/api/system/shutdown", tags=["system"])
    def api_shutdown(body: ShutdownIn) -> dict:
        need_confirm(body.confirm, "Stopping the server")
        store.audit("server.shutdown", None, {"stop_jobs": body.stop_jobs})
        running = sup.shutdown(stop_workers=body.stop_jobs, wait=20 if body.stop_jobs else 0)
        srv = app.state.server
        if srv is not None:
            srv.should_exit = True
        return {"stopping": True, "workers_still_running": [{"id": j["id"], "name": j["name"], "status": j["status"]} for j in running],
                "message": ("Download workers keep running independently of the web server and continue to write their databases; "
                            "start the app again to see them." if running else "No download worker is running.")}

    # ============================================================================ jobs
    @app.post("/api/plan", tags=["jobs"], response_model=PlanOut)
    def api_plan(spec: J.JobSpec) -> dict:
        return sup.preview(spec)

    @app.get("/api/jobs", tags=["jobs"], response_model=list[JobOut])
    def api_jobs(status: str | None = None) -> list[dict]:
        return sup.list_jobs(status.split(",") if status else None)

    @app.get("/api/jobs/{job_id}", tags=["jobs"], response_model=JobOut)
    def api_job(job_id: int) -> dict:
        return sup.job_view(sup.get(job_id))

    @app.post("/api/jobs", tags=["jobs"], response_model=dict)
    def api_create_job(body: CreateJobIn) -> dict:
        need_confirm(body.confirm, "Adding a job")
        plan = None
        if body.spec.kind == "download":
            try:
                p = sup.preview(body.spec)
                if p["blockers"] and body.start == "now":
                    raise JobError("Cannot start now: " + "; ".join(b["message"] for b in p["blockers"]), 409)
                plan = {k: p[k] for k in ("instruments", "instruments_with_work", "windows", "resolved_start", "end_exclusive",
                                          "window_days", "estimated_seconds", "rps")}
            except DatabaseBusy:
                plan = None            # the database is being written; the job keeps its own plan once it starts
        job = sup.create_job(body.spec, plan=plan)
        reason = None
        if body.start == "now":
            job, reason = sup.start_now(job["id"])
        return {"job": sup.job_view(job), "started": job["status"] in ACTIVE, "waiting_reason": reason}

    @app.post("/api/jobs/resume-eligible", tags=["jobs"])
    def api_resume_eligible(body: ConfirmIn = Body(default_factory=ConfirmIn)) -> dict:
        need_confirm(body.confirm, "Resuming every paused, interrupted, stopped or failed job (they will spend API requests)")
        jobs = sup.resume_eligible()
        sup.wake()
        return {"resumed": [j["id"] for j in jobs], "jobs": jobs}

    @app.post("/api/jobs/pause-all", tags=["jobs"])
    def api_pause_all() -> dict:
        """Graceful: every active worker finishes its in-flight window and exits. Nothing is killed."""
        jobs = sup.pause_all()
        sup.wake()
        return {"pausing": [j["id"] for j in jobs], "jobs": jobs}

    @app.post("/api/jobs/reorder", tags=["jobs"])
    def api_reorder(ids: list[int] = Body(embed=True)) -> list[dict]:
        return sup.reorder(ids)

    @app.post("/api/jobs/{job_id}/{action}", tags=["jobs"])
    def api_job_action(job_id: int, action: Literal["start", "pause", "stop", "resume", "cancel", "abandon", "kill", "retry_failed"],
                       body: JobActionIn = Body(default_factory=JobActionIn)) -> dict:
        reason = None
        if action == "start":
            job, reason = sup.start_now(job_id)
        elif action == "pause":
            job = sup.pause(job_id)
        elif action == "stop":
            job = sup.stop(job_id, reason="stop")
        elif action == "resume":
            job = sup.resume(job_id)
        elif action == "cancel":
            job = sup.cancel(job_id)
        elif action == "abandon":
            need_confirm(body.confirm, "Abandoning the remaining work of a job")
            job = sup.cancel(job_id, abandon=True)
        elif action == "kill":
            need_confirm(body.confirm, "Force-stopping a worker")
            job = sup.kill(job_id)
        else:
            need_confirm(body.confirm, "Retrying failed windows (this spends API requests)")
            job = sup.retry_failed(job_id, security_ids=body.security_ids, empty_error_codes=body.empty_error_codes,
                                   confirm_full=body.confirm_full)
        sup.wake()
        return {"job": sup.job_view(job), "waiting_reason": reason}

    @app.delete("/api/jobs/{job_id}", tags=["jobs"])
    def api_delete_job(job_id: int) -> dict:
        sup.delete(job_id)
        return {"deleted": job_id}

    @app.get("/api/jobs/{job_id}/events", tags=["jobs"])
    def api_events(job_id: int, limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
        sup.get(job_id)
        return store.all("SELECT ts, level, type, message FROM job_events WHERE job_id = ? ORDER BY id DESC LIMIT ?", (job_id, limit))

    @app.get("/api/jobs/{job_id}/runs", tags=["jobs"])
    def api_runs(job_id: int) -> list[dict]:
        sup.get(job_id)
        return [{k: r[k] for k in ("seq", "started_at", "ended_at", "pid", "exit_code", "engine_status", "outcome", "attempts_used")}
                | {"argv": redact(r["argv"] or "")} for r in store.all("SELECT * FROM job_runs WHERE job_id = ? ORDER BY seq DESC", (job_id,))]

    @app.get("/api/jobs/{job_id}/log", tags=["logs"])
    def api_log(job_id: int, tail: int = Query(200, ge=1, le=5000), offset: int | None = Query(None, ge=0),
                run: int | None = None) -> dict:
        return sup.read_log(job_id, run_seq=run, tail=tail, offset=offset)

    @app.get("/api/jobs/{job_id}/log/download", tags=["logs"])
    def api_log_download(job_id: int, run: int | None = None):
        path = sup.log_path(job_id, run)
        if path is None or not path.exists():
            raise KeyError("no log exists for this job yet")

        def gen():
            with open(path, "rb") as fh:
                for line in fh:
                    yield redact(line.decode("utf-8", "replace")).encode()
        return StreamingResponse(gen(), media_type="text/plain; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="job{job_id}_run{run or "latest"}.log"'})

    @app.get("/api/activity", tags=["logs"])
    def api_activity() -> dict:
        active = [j for j in sup.list_jobs() if j["status"] in ACTIVE]
        for j in active:
            j["log_tail"] = sup.read_log(j["id"], tail=40)["text"]
            j["recent_events"] = store.all("SELECT ts, level, type, message FROM job_events WHERE job_id = ? ORDER BY id DESC LIMIT 15", (j["id"],))
        usage = sup.usage()
        recent = store.all("SELECT job_id, ts, level, type, message FROM job_events ORDER BY id DESC LIMIT 60")
        interesting = [e for e in recent if e["type"] in ("started", "finished", "interrupted", "adopted", "resumed", "paused", "pause_requested",
                                                           "stop_requested", "cancelled", "recovery", "auto_resumed", "spawn_failed")]
        auth = [e for e in recent if "token" in e["message"].lower() or "authentication" in e["message"].lower()]
        return {"active": active, "events": interesting[:30], "auth_events": auth[:10], "limit_events": usage["limit_events"],
                "usage": usage, "queue_enabled": sup.effective()["queue_enabled"],
                "idle_note": None if active else "No download worker is running."}

    @app.post("/api/queue", tags=["jobs"])
    def api_queue(enabled: bool = Body(embed=True)) -> dict:
        sup.update_settings(queue_enabled=enabled)
        return {"queue_enabled": enabled}

    # ---- campaigns
    @app.get("/api/campaigns", tags=["campaigns"])
    def api_campaigns() -> list[dict]:
        return sup.list_campaigns()

    @app.post("/api/campaigns", tags=["campaigns"])
    def api_create_campaign(body: CampaignIn) -> dict:
        need_confirm(body.confirm, "Creating a campaign")
        return sup.create_campaign(body.name, body.jobs, continue_on_warnings=body.continue_on_warnings, start_after=body.start_after)

    @app.post("/api/campaigns/{campaign_id}/{action}", tags=["campaigns"])
    def api_campaign_action(campaign_id: int, action: Literal["pause", "resume", "cancel"], body: ConfirmIn = Body(default_factory=ConfirmIn)) -> dict:
        if action != "pause":
            need_confirm(body.confirm, f"{'Resuming' if action == 'resume' else 'Cancelling'} a campaign")
        return sup.campaign_action(campaign_id, action)

    @app.delete("/api/campaigns/{campaign_id}", tags=["campaigns"])
    def api_delete_campaign(campaign_id: int) -> dict:
        sup.delete_campaign(campaign_id)
        return {"deleted": campaign_id}

    # ============================================================================ data
    @app.get("/api/catalog", tags=["data"])
    def api_catalog(refresh: bool = False) -> dict:
        return {tf: data.summary(tf, force=refresh) for tf in TIMEFRAMES}

    @app.get("/api/instruments", tags=["data"])
    def api_instruments(q: str = "", has_data: Literal["any", "daily", "15min", "both", "none"] = "any",
                        sort: str = "symbol", desc: bool = False, page: int = Query(1, ge=1),
                        page_size: int = Query(100, ge=1, le=1000), refresh: bool = False) -> dict:
        master = data.instrument_master()
        cov = {tf: data.instrument_coverage(tf, force=refresh) for tf in TIMEFRAMES}
        by = {tf: {r["security_id"]: r for r in (cov[tf].get("rows") or [])} for tf in TIMEFRAMES}
        rows = []
        for r in master.get("rows", []):
            d, i = by["daily"].get(r["security_id"]), by["15min"].get(r["security_id"])
            rows.append({**r, "daily": {k: d[k] for k in ("candles", "first", "last", "failed_windows", "retriable_windows")} if d else None,
                         "intraday": {k: i[k] for k in ("candles", "first", "last", "failed_windows", "retriable_windows")} if i else None})
        ql = q.strip().upper()
        if ql:
            rows = [r for r in rows if ql in (r["symbol"] or "").upper() or ql in (r["name"] or "").upper() or ql == str(r["security_id"])]
        has_d = lambda r: bool(r["daily"] and (r["daily"]["candles"] or 0) > 0)
        has_i = lambda r: bool(r["intraday"] and (r["intraday"]["candles"] or 0) > 0)
        rows = {"any": rows, "daily": [r for r in rows if has_d(r)], "15min": [r for r in rows if has_i(r)],
                "both": [r for r in rows if has_d(r) and has_i(r)], "none": [r for r in rows if not has_d(r) and not has_i(r)]}[has_data]
        keyf = {"symbol": lambda r: r["symbol"] or "", "security_id": lambda r: r["security_id"],
                "daily_candles": lambda r: (r["daily"] or {}).get("candles") or 0,
                "intraday_candles": lambda r: (r["intraday"] or {}).get("candles") or 0,
                "daily_last": lambda r: (r["daily"] or {}).get("last") or "",
                "intraday_last": lambda r: (r["intraday"] or {}).get("last") or ""}.get(sort)
        if keyf is None:
            raise ValueError(f"cannot sort by {sort!r}")
        rows.sort(key=keyf, reverse=desc)
        total = len(rows)
        return {"total": total, "page": page, "page_size": page_size, "rows": rows[(page - 1) * page_size: page * page_size],
                "universe_total": len(master.get("rows", [])), "master_source": master.get("source"),
                "coverage": {tf: {"source": cov[tf].get("source"), "cached_at": cov[tf].get("cached_at"), "stale": cov[tf].get("stale"),
                                  "reason": cov[tf].get("stale_reason")} for tf in TIMEFRAMES}}

    @app.get("/api/instruments/search", tags=["data"])
    def api_search(q: str = "", limit: int = Query(15, ge=1, le=50)) -> list[dict]:
        return data.search(q, limit)

    @app.get("/api/instruments/{security_id}", tags=["data"])
    def api_instrument(security_id: int) -> dict:
        inst = data.resolve_instrument(security_id)
        if inst is None:
            raise KeyError(f"security id {security_id} is not in the loaded universe")
        cov = {}
        for tf in TIMEFRAMES:
            c = data.instrument_coverage(tf)
            row = next((r for r in c.get("rows") or [] if r["security_id"] == security_id), None)
            cov[tf] = {"row": row, "source": c.get("source"), "stale": c.get("stale"), "reason": c.get("stale_reason")}
        related = [j for j in sup.list_jobs() if (j["selection"] or {}).get("mode") == "all"
                   or inst["symbol"] in ((j["selection"] or {}).get("symbols") or [])][:20]
        return {"instrument": inst, "coverage": cov, "related_jobs": [{"id": j["id"], "name": j["name"], "status": j["status"],
                                                                        "timeframe": j["timeframe"]} for j in related]}

    @app.post("/api/instruments/refresh", tags=["data"])
    def api_refresh_master(confirm: bool = Body(embed=True, default=False)) -> dict:
        need_confirm(confirm, "Refreshing the instrument master (downloads Dhan's public master CSV and rewrites the instrument tables of both databases)")
        job = sup.create_job(J.JobSpec(kind="instruments_load"), allow_duplicate=True)
        job, reason = sup.start_now(job["id"])
        return {"job": sup.job_view(job), "waiting_reason": reason}

    @app.get("/api/candles", tags=["data"])
    def api_candles(timeframe: Timeframe, security_id: int, start: date | None = None, end: date | None = None,
                    bars: int | None = Query(None, ge=1, le=150_000)) -> dict:
        return data.candles(timeframe, security_id, start=start, end=end, bars=bars)

    @app.get("/api/candles/table", tags=["data"])
    def api_candle_table(timeframe: Timeframe, security_id: int, start: date | None = None, end: date | None = None,
                         sort: str = "time", desc: bool = True, page: int = Query(1, ge=1),
                         page_size: int = Query(100, ge=1, le=1000)) -> dict:
        return data.table(timeframe, security_id, start=start, end=end, sort=sort, desc=desc, page=page, page_size=page_size)

    @app.get("/api/candles/export.csv", tags=["data"])
    def api_candle_csv(timeframe: Timeframe, security_id: int, start: date | None = None, end: date | None = None):
        inst = data.resolve_instrument(security_id)
        if inst is None:
            raise KeyError(f"security id {security_id} is not in the loaded universe")
        # open the gate first so a busy database is reported as a clean error instead of a broken download
        with data.connect(timeframe):
            pass
        name = f"{inst['symbol']}_{timeframe}_{start or 'start'}_{end or 'end'}.csv"
        return StreamingResponse(data.export_csv(timeframe, security_id, start=start, end=end), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ---- groups
    @app.get("/api/groups", tags=["data"])
    def api_groups() -> list[dict]:
        import json as _json
        return [{"id": g["id"], "name": g["name"], "count": len(_json.loads(g["security_ids"])), "security_ids": _json.loads(g["security_ids"]),
                 "created_at": g["created_at"]} for g in store.all("SELECT * FROM symbol_groups ORDER BY name")]

    @app.post("/api/groups", tags=["data"])
    def api_create_group(body: GroupIn) -> dict:
        import json as _json
        from stockdata.web.store import now
        master = {r["security_id"] for r in data.instrument_master().get("rows", [])}
        unknown = [i for i in body.security_ids if i not in master]
        if unknown:
            raise ValueError(f"security ids not in the loaded universe: {unknown[:10]}")
        try:
            cur = store.execute("INSERT INTO symbol_groups (name, security_ids, created_at) VALUES (?, ?, ?)",
                                (body.name.strip(), _json.dumps(list(dict.fromkeys(body.security_ids))), now()))
        except Exception:
            raise JobError(f"a group named {body.name!r} already exists", 409)
        store.audit("group.create", f"group:{cur.lastrowid}", {"name": body.name, "count": len(body.security_ids)})
        return {"id": cur.lastrowid, "name": body.name, "count": len(set(body.security_ids))}

    @app.delete("/api/groups/{group_id}", tags=["data"])
    def api_delete_group(group_id: int) -> dict:
        store.execute("DELETE FROM symbol_groups WHERE id = ?", (group_id,))
        store.audit("group.delete", f"group:{group_id}", {})
        return {"deleted": group_id}

    # ============================================================================ quality
    @app.get("/api/quality/{timeframe}", tags=["quality"])
    def api_quality(timeframe: Timeframe) -> dict:
        return quality.state(timeframe)

    @app.post("/api/quality/{timeframe}/run", tags=["quality"])
    def api_quality_run(timeframe: Timeframe) -> dict:
        return quality.start(timeframe)

    @app.post("/api/quality/{timeframe}/cancel", tags=["quality"])
    def api_quality_cancel(timeframe: Timeframe) -> dict:
        quality.cancel(timeframe)
        return quality.state(timeframe)

    @app.get("/api/quality/{timeframe}/coverage.csv", tags=["quality"])
    def api_coverage_csv(timeframe: Timeframe):
        df = coverage_csv(data, timeframe)
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        saved = settings.reports_dir / f"coverage_{timeframe}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        df.to_csv(saved, index=False)
        store.audit("report.coverage_csv", timeframe, {"saved": str(saved)})
        return FileResponse(saved, media_type="text/csv", filename=saved.name)

    # ============================================================================ settings
    def settings_view() -> dict:
        eff = sup.effective()
        log_files = sorted(settings.log_dir.glob("server.log*")) if settings.log_dir.exists() else []
        return {
            "version": settings.version, "commit": settings.commit,
            "paths": {"data_dir": str(settings.data_dir), "data_dir_source": settings.data_dir_source,
                      "daily_db": str(data.path("daily")), "intraday_db": str(data.path("15min")),
                      "ledger": str(settings.engine.request_ledger_path), "control_db": str(settings.control_db),
                      "jobs_dir": str(settings.jobs_dir), "server_log": str(settings.log_dir / "server.log"),
                      "engine_logs": str(settings.data_dir / "logs"), "reports": str(settings.reports_dir),
                      "env_file": str(settings.credentials_file)},
            "files": {tf: data.file_info(tf) for tf in TIMEFRAMES},
            "limits": {"max_per_run": eff["max_per_run"], "max_per_day": eff["max_per_day"], "rps": eff["rps"],
                       "documented_account_limit": 100_000, "documented_rps": 5},
            "behaviour": {"parallel_timeframes": eff["parallel_timeframes"], "queue_enabled": eff["queue_enabled"],
                          "auto_resume_budget": eff["auto_resume_budget"], "min_free_gb": eff["min_free_gb"]},
            "disk": disk(), "bind": {"host": settings.host, "port": settings.port},
            "logs": {"server_log_files": [{"name": p.name, "bytes": p.stat().st_size} for p in log_files],
                     "retention": "server log rotates at 5 MB and keeps 5 files; job logs are kept with the job until it is removed from the history"},
            "pending_data_dir": read_bootstrap(settings.repo_root).get("data_dir"),
            "notes": ["Limit and rate changes apply to jobs started after the change.",
                      "The data directory is fixed while the server runs; changing it takes effect after a restart."],
        }

    @app.get("/api/settings", tags=["settings"])
    def api_settings() -> dict:
        return settings_view()

    @app.post("/api/settings/validate", tags=["settings"])
    def api_validate_settings(body: SettingsIn) -> dict:
        """Check a settings change without saving it."""
        cur = {**sup.effective(), **body.model_dump(exclude_none=True)}
        problems, notes = [], []
        try:
            from stockdata.budget import validate_limits
            validate_limits(int(cur["max_per_run"]), int(cur["max_per_day"]))
        except ValueError as exc:
            problems.append(str(exc))
        if cur["max_per_run"] > cur["max_per_day"]:
            notes.append("The per-run cap is higher than the per-day cap; the day cap will stop a run first.")
        if cur["max_per_day"] > 90_000:
            notes.append("A daily cap this close to Dhan's 100,000 leaves little room for other programs using the same account.")
        if cur["rps"] > 4:
            notes.append("More than 4 requests/second leaves little margin below Dhan's documented 5/s.")
        if cur["parallel_timeframes"]:
            notes.append("With parallel timeframes each worker is limited to 2 requests/second.")
        return {"ok": not problems, "problems": problems, "notes": notes, "effective_after": cur,
                "applies_to": "jobs started after you save; a running job keeps the limits it started with"}

    @app.put("/api/settings", tags=["settings"])
    def api_put_settings(body: SettingsIn) -> dict:
        sup.update_settings(**body.model_dump(exclude_none=True))
        return settings_view()

    @app.post("/api/settings/validate-data-dir", tags=["settings"])
    def api_validate_dir(body: DataDirIn) -> dict:
        p = Path(body.path).expanduser()
        if not p.is_absolute():
            raise ValueError("give an absolute path (for example C:\\StockData\\data)")
        p = p.resolve()
        exists = p.exists()
        if exists and not p.is_dir():
            raise ValueError("that path is a file, not a folder")
        present = {tf: (p / TIMEFRAMES[tf].db_name).exists() for tf in TIMEFRAMES}
        ledger = (p / "request_ledger.sqlite").exists()
        probe = p
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        writable = os.access(probe, os.W_OK)
        same = p == settings.data_dir
        return {"path": str(p), "exists": exists, "is_current": same, "databases_present": present, "ledger_present": ledger,
                "writable": writable, "free_bytes": shutil.disk_usage(probe).free,
                "warning": None if (same or any(present.values())) else
                "This folder holds no stockdata databases. Switching to it would start a NEW EMPTY environment (new databases, a new "
                "request ledger and an empty job history); your existing data would stay where it is, untouched."
                + (" A new request ledger would not know today's request count." if not ledger else "")}

    @app.put("/api/settings/data-dir", tags=["settings"])
    def api_set_data_dir(body: DataDirIn) -> dict:
        info = api_validate_dir(body)
        need_confirm(body.confirm, "Changing the data directory")
        if info["warning"] and not body.confirm_new_empty:
            raise JobError("This would create a new empty environment: " + info["warning"] + " Confirm with confirm_new_empty.", 400)
        if not info["writable"]:
            raise JobError("that location is not writable", 400)
        boot = read_bootstrap(settings.repo_root)
        boot["data_dir"] = info["path"]
        write_bootstrap(boot, settings.repo_root)
        store.audit("settings.data_dir", None, {"to": info["path"], "from": str(settings.data_dir)})
        return {"saved": True, "restart_required": True, "new_data_dir": info["path"], "current_data_dir": str(settings.data_dir),
                "message": f"Saved to {BOOTSTRAP_FILE}. Nothing changes until the server is restarted; running downloads keep using the current folder."}

    @app.get("/api/credentials", tags=["settings"])
    def api_creds() -> dict:
        return creds.status()

    @app.put("/api/credentials", tags=["settings"])
    def api_put_creds(body: CredentialsIn) -> dict:
        try:
            status = creds.update(access_token=body.access_token, client_id=body.client_id)
        except ValueError as exc:
            raise JobError(str(exc), 400)
        store.audit("credentials.update", None, {"access_token": bool(body.access_token), "client_id": bool(body.client_id)})
        sup.wake()
        return status

    @app.get("/api/logs/server", tags=["logs"])
    def api_server_log(tail: int = Query(200, ge=1, le=5000)) -> dict:
        path = settings.log_dir / "server.log"
        if not path.exists():
            return {"text": "", "path": str(path)}
        data_ = path.read_bytes()[-400_000:].decode("utf-8", "replace").splitlines()[-tail:]
        return {"text": redact("\n".join(data_)), "path": str(path)}

    @app.get("/api/audit", tags=["settings"])
    def api_audit(limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
        return store.all("SELECT ts, action, subject, details FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # ============================================================================ system health, recovery, maintenance
    @app.get("/api/system/dependencies", tags=["system"])
    def api_dependencies() -> dict:
        return sysinfo.dependencies()

    @app.get("/api/system/databases", tags=["system"])
    def api_database_health() -> dict:
        return sysinfo.databases()

    @app.get("/api/system/findings", tags=["system"])
    def api_findings() -> list[dict]:
        return sysinfo.findings()

    @app.get("/api/system/diagnostics", tags=["system"])
    def api_diagnostics():
        store.audit("system.diagnostics", None, {})
        return PlainTextResponse(sysinfo.bundle(), headers={"Content-Disposition": 'attachment; filename="stockdata_diagnostics.txt"'})

    @app.post("/api/system/reconcile", tags=["system"])
    def api_reconcile() -> dict:
        return sup.reconcile()

    @app.get("/api/system/orphans", tags=["system"])
    def api_orphans() -> list[dict]:
        return sup.orphans()

    @app.post("/api/system/orphans/{pid}/kill", tags=["system"])
    def api_kill_orphan(pid: int, body: ConfirmIn) -> dict:
        need_confirm(body.confirm, "Force-stopping an untracked download worker")
        return sup.kill_orphan(pid)

    @app.get("/api/maintenance", tags=["system"])
    def api_maintenance() -> dict:
        return maint.status()

    @app.post("/api/maintenance/{timeframe}/recover", tags=["system"])
    def api_recover(timeframe: Timeframe, body: ConfirmIn) -> dict:
        need_confirm(body.confirm, "Recovering a database (replaying its write-ahead log)")
        return maint.recover(timeframe)

    @app.post("/api/maintenance/{timeframe}/backup", tags=["system"])
    def api_backup(timeframe: Timeframe, body: ConfirmIn) -> dict:
        need_confirm(body.confirm, "Backing up a database (copies the whole file)")
        return maint.start_backup(timeframe)

    @app.delete("/api/maintenance/backups/{name}", tags=["system"])
    def api_delete_backup(name: str, confirm: bool = Query(False)) -> dict:
        need_confirm(confirm, "Deleting a backup")
        maint.delete_backup(name)
        return {"deleted": name}

    # ============================================================================ single-page app
    if settings.static_dir:
        app.mount("/", StaticFiles(directory=str(settings.static_dir), html=True), name="ui")
    else:
        @app.get("/", include_in_schema=False)
        def no_ui():
            return JSONResponse({"message": "The web interface has not been built yet. Run build_frontend.bat (or "
                                            "'npm ci && npm run build' in webui/), or open /api/docs for the API."}, status_code=200)

    return app
