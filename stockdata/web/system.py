"""System health, dependency health, troubleshooting findings and the redacted diagnostics bundle."""
from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone

import duckdb

from stockdata.timeframes import TIMEFRAMES, get_timeframe
from stockdata.web import bootstrap
from stockdata.web.data import DataAccess, DatabaseBusy
from stockdata.web.redact import redact
from stockdata.web.settings import WebSettings
from stockdata.web.supervisor import Supervisor, STALE_AFTER
from stockdata.web.credentials import CredentialStore

WAL_WARN_BYTES = 64 * 1024 ** 2


class SystemInfo:
    def __init__(self, settings: WebSettings, sup: Supervisor, data: DataAccess, creds: CredentialStore):
        self.s, self.sup, self.data, self.creds = settings, sup, data, creds

    # ------------------------------------------------------------------ dependencies
    def dependencies(self) -> dict:
        env = bootstrap.environment(self.s.repo_root / bootstrap.REQUIREMENTS, self.s.repo_root / "webui" / "dist")
        env["platform"] = platform.platform()
        env["duckdb"] = duckdb.__version__
        env["node_required"] = False
        env["note"] = ("Node.js is not needed to run the application: the interface is prebuilt in webui/dist. "
                       "Only developers who change webui/src need it (tools/dev/build_frontend.bat).")
        return env

    # ------------------------------------------------------------------ databases
    def databases(self) -> dict:
        out = {}
        for tf in TIMEFRAMES:
            info = self.data.file_info(tf)
            row = {**info, "writer_job_id": self.sup.writer_for(tf), "maintenance": self.data.gate.maintenance(tf),
                   "readable": None, "identity": None, "schema_version": None, "problem": None}
            if info["exists"] and row["writer_job_id"] is None and row["maintenance"] is None:
                try:
                    with self.data.connect(tf) as con:
                        meta = dict(con.execute("SELECT key, value FROM download_meta").fetchall())
                    row.update(readable=True, identity=meta.get("timeframe"), schema_version=meta.get("schema_version"))
                    if meta.get("timeframe") != get_timeframe(tf).name:
                        row["problem"] = f"the file identifies itself as {meta.get('timeframe')!r}, not {tf!r}"
                except DatabaseBusy as busy:
                    row["problem"] = str(busy)
                except Exception as exc:                       # noqa: BLE001 - this is the health report
                    row.update(readable=False, problem=redact(f"{type(exc).__name__}: {exc}")[:300])
            out[tf] = row
        return out

    # ------------------------------------------------------------------ findings
    def findings(self) -> list[dict]:
        f: list[dict] = []

        def add(level, title, detail, route=None, action=None):
            f.append({"level": level, "title": title, "detail": detail, "route": route, "action": action})

        sup = self.sup
        t = sup._thread
        if not (t and t.is_alive()):
            add("error", "The job scheduler is not running", "Queued jobs will not start. Restart the application (stop_stockdata.bat, then start_stockdata.bat).")
        dep = self.dependencies()
        if not dep["python_ok"]:
            add("error", "Python is older than required", f"Running {dep['python']}, need {dep['min_python']} or newer.")
        for p in dep["packages"]:
            if not p["ok"]:
                add("error", f"Package {p['package']} is missing or outdated", f"Need {p['required']}, installed: {p['installed'] or 'none'}. start_stockdata.bat installs it on the next start.")
        if not dep["frontend"]["ok"]:
            add("warn", "The prebuilt interface has problems", "; ".join(dep["frontend"]["problems"]))
        cred = self.creds.status()
        if not cred["ready"]:
            add("error", "Dhan credentials are not configured", "No download can start.", "/settings", "Open credentials")
        elif cred["access_token"].get("expired"):
            add("error", "The Dhan access token has expired", f"It expired at {cred['access_token']['expires_at_ist']} IST. Replace it, then resume stopped jobs.", "/settings", "Replace the token")
        elif cred["access_token"].get("seconds_left") is not None and cred["access_token"]["seconds_left"] < 7200:
            add("warn", "The Dhan access token expires soon", f"At {cred['access_token']['expires_at_ist']} IST. A long download will stop cleanly then.", "/settings", "Replace the token")
        from stockdata.web.process import free_bytes
        free = free_bytes(self.s.data_dir)
        low = sup.effective()["min_free_gb"] * 1024 ** 3
        if free < low:
            add("error" if free < low / 2 else "warn", "Free disk space is low", f"{free / 1e9:.1f} GB free; the warning level is {low / 1e9:.0f} GB.")
        for tf, d in self.databases().items():
            if d["problem"]:
                add("error", f"The {tf} database has a problem", d["problem"], "/settings", "Open maintenance")
            if d["exists"] and d["wal_bytes"] > WAL_WARN_BYTES and d["writer_job_id"] is None:
                add("warn", f"The {tf} database has a large pending write-ahead log ({d['wal_bytes'] / 1e6:.0f} MB)",
                    "This is normal right after a crash or kill. Run 'Recover (replay log)' in Settings → Maintenance.", "/settings", "Open maintenance")
        for o in sup.orphans():
            add("error", f"A download worker (pid {o['pid']}) is running that no job accounts for",
                "It may be writing a database. Stop it from Settings → Maintenance if you did not start it.", "/settings", "Open maintenance")
        counts = sup.counts()
        if counts["failed"] or counts["interrupted"] or counts["paused"]:
            add("warn", "Jobs need your attention", f"{counts['failed']} failed, {counts['interrupted']} interrupted/stopped, {counts['paused']} paused.", "/queue", "Open the queue")
        for j in sup.list_jobs():
            if j["worker"] and j["worker"]["stale"]:
                add("warn", f"Job #{j['id']} has not reported for over {STALE_AFTER // 60} minutes", "The worker process is alive but silent. Check its log; Force stop and Resume if it is hung.", f"/queue?job={j['id']}", "Open the job")
        if not sup.effective()["queue_enabled"]:
            add("info", "Queue processing is switched off", "Queued jobs wait until you switch it on in the Job Queue page.", "/queue", "Open the queue")
        usage = sup.usage()
        if usage["remaining_day"] < 5000:
            add("warn", "The daily request budget is nearly used up", f"{usage['remaining_day']:,} attempts remain today; it resets at IST midnight.", "/activity", "Open usage")
        if not f:
            add("ok", "No problems found", "Backend, scheduler, packages, credentials, disk space and databases all look healthy.")
        return f

    # ------------------------------------------------------------------ diagnostics bundle
    def bundle(self) -> str:
        sup, s = self.sup, self.s
        dep = self.dependencies()
        lines = [f"Stock Data Management Center diagnostics - {datetime.now(timezone.utc).isoformat(timespec='seconds')}", "",
                 f"version {s.version}, commit {s.commit}", f"python {dep['python']} ({dep['executable']}), {dep['platform']}, duckdb {dep['duckdb']}",
                 f"data directory: {s.data_dir}  [{s.data_dir_source}]", f"ledger: {s.engine.request_ledger_path}",
                 f"control database: {s.control_db}", f"listening on {s.host}:{s.port}", "", "== dependencies =="]
        lines += [f"  {'ok ' if p['ok'] else 'BAD'} {p['package']} {p['required']} (installed {p['installed']})" for p in dep["packages"]]
        lines += [f"  interface: {'ok' if dep['frontend']['ok'] else 'PROBLEM ' + '; '.join(dep['frontend']['problems'])}", "", "== databases =="]
        for tf, d in self.databases().items():
            lines.append(f"  {tf}: exists={d['exists']} size={d['size_bytes']} wal={d['wal_bytes']} writer_job={d['writer_job_id']} "
                         f"maintenance={d['maintenance']} readable={d['readable']} problem={d['problem']}")
        lines += ["", "== findings =="] + [f"  [{x['level']}] {x['title']}: {x['detail']}" for x in self.findings()]
        usage = sup.usage()
        lines += ["", "== request usage ==", f"  today {usage['used_today']} of {usage['max_per_day']} (per run {usage['max_per_run']}); by label {usage['by_label']}",
                  f"  recent limit events: {usage['limit_events'][:5]}", "", "== credentials (masked) ==", f"  {self.creds.status()}",
                  "", "== jobs ==", f"  counts {sup.counts()['by_status']}"]
        for j in sup.list_jobs(limit=25):
            lines.append(f"  #{j['id']} {j['name']} [{j['status']}] windows {j['windows_done']}/{j['windows_planned']} attempts {j['attempts_used']} "
                         f"exit={j['exit_code']} note={j['note']} error={j['last_error']} next={j['next_action']}")
        lines += ["", "== recent events =="]
        for e in sup.store.all("SELECT job_id, ts, level, type, message FROM job_events ORDER BY id DESC LIMIT 40"):
            lines.append(f"  {e['ts']} job={e['job_id']} {e['level']} {e['type']}: {e['message']}")
        log = s.log_dir / "server.log"
        lines += ["", "== server log (last 120 lines) =="]
        if log.exists():
            lines += ["  " + l for l in log.read_bytes()[-60_000:].decode("utf-8", "replace").splitlines()[-120:]]
        for j in sup.list_jobs(limit=3):
            tail = sup.read_log(j["id"], tail=40)["text"]
            if tail:
                lines += ["", f"== job #{j['id']} log (last 40 lines) =="] + ["  " + l for l in tail.splitlines()]
        return redact("\n".join(lines) + "\n")
