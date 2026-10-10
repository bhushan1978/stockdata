"""Windows Task Scheduler integration for the automatic updates.

Design choices (each one is a way scheduled data jobs usually go wrong):

* **The task runs a command line, not the web interface.** ``python -m stockdata.web automation run ...`` is a headless
  entry point that needs no browser and starts no web server. The command line contains paths and phase names only; the
  credentials are read from the untracked ``.env`` file at run time, so nothing secret is ever part of a task definition,
  a log line or ``schtasks`` output.
* **Asia/Kolkata, whatever the PC's time zone.** The trigger's start time carries the ``+05:30`` offset, which makes Task
  Scheduler keep it synchronised across time zones: it fires at the same UTC instant every day and never shifts with the
  machine's own daylight-saving rules.
* **Missed schedules recover on their own.** ``StartWhenAvailable`` runs a missed task as soon as the machine is back
  (after a restart, a shutdown, sleep or a closed lid), and an update is incremental from each instrument's last stored
  candle, so a late run simply catches up the missed days; it never needs the original time.
* **No overlaps.** ``MultipleInstancesPolicy=IgnoreNew`` here, and a lock inside the runner.
* **Two small tasks, not one clever one:** the post-close update and the later reconciliation, each with an explicit phase.

The Windows calls are made through ``schtasks.exe`` and PowerShell with fixed argument lists (never a shell string), behind a
small backend interface so everything except the final process call is tested on any operating system. The Windows backend
itself could not be executed in the development environment (Linux); see the documentation.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
FOLDER = "\\StockDataMC"
TASKS = {"post-close": "Daily Update", "reconcile": "Reconciliation"}
NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,60}$")
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# LastTaskResult values worth explaining (the rest are the runner's own exit codes, see RUNNER_EXIT)
SCHEDULER_RESULTS = {0: "the last run finished successfully", 267009: "the task is running now", 267011: "the task has not run yet",
                     267014: "the last run was stopped by the scheduler (time limit or manual end)", 2147750687: "an instance was already running"}
RUNNER_EXIT = {0: "completed", 2: "completed with warnings", 3: "ACTION REQUIRED: a credential was rejected or has expired",
               4: "stopped at the daily request budget", 5: "stopped on request", 6: "stopped: not enough free disk space",
               7: "stopped: a critical integrity check failed", 8: "not run: the application server is not responding",
               75: "skipped: another automatic run was already in progress"}


@dataclass
class TaskSpec:
    phase: str                            # 'post-close' | 'reconcile'
    time_ist: str                         # 'HH:MM' Asia/Kolkata
    weekdays: list[int]                   # 0 = Monday
    python: str
    repo: str
    data_dir: str
    env_file: str | None = None
    run_whether_logged_on: bool = False   # False: only while the user is logged on (the safe default; no stored password)
    wake_to_run: bool = False
    user: str | None = None
    execution_limit_hours: int = 8
    description: str = ""
    start_day: str | None = None          # IST date (YYYY-MM-DD) the schedule starts from

    def validate(self) -> None:
        if self.phase not in TASKS:
            raise ValueError(f"unknown phase {self.phase!r}")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", self.time_ist):
            raise ValueError("the time must be HH:MM (24-hour, Asia/Kolkata)")
        if not self.weekdays or any(d not in range(7) for d in self.weekdays):
            raise ValueError("choose at least one weekday")
        for label, value in (("python", self.python), ("repo", self.repo), ("data_dir", self.data_dir)):
            if not value or "\n" in value or "\r" in value or '"' in value:
                raise ValueError(f"invalid {label} path")


def task_path(phase: str) -> str:
    return f"{FOLDER}\\{TASKS[phase]}"


def arguments_for(spec: TaskSpec) -> str:
    """The task's command-line arguments: paths and phase names only. Never a credential."""
    parts = ["-m", "stockdata.web", "--data-dir", f'"{spec.data_dir}"', "automation", "run",
             "--phase", spec.phase, "--trigger", "scheduled"]
    return " ".join(parts)


def build_task_xml(spec: TaskSpec, *, now: datetime | None = None) -> str:
    spec.validate()
    now = now or datetime.now(IST)
    start_day = spec.start_day or now.astimezone(IST).date().isoformat()
    boundary = f"{start_day}T{spec.time_ist}:00+05:30"            # the offset is what makes the trigger time-zone independent
    user = spec.user or _current_user()
    logon = "S4U" if spec.run_whether_logged_on else "InteractiveToken"
    days = "".join(f"<{DAY_NAMES[d]} />" for d in sorted(set(spec.weekdays)))
    args = escape(arguments_for(spec))
    desc = escape(spec.description or f"Stock Data Management Center: {TASKS[spec.phase]} ({spec.time_ist} IST). Headless: no browser or web server is needed.")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{NS}">
  <RegistrationInfo>
    <Description>{desc}</Description>
    <URI>{escape(task_path(spec.phase))}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{boundary}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>{days}</DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>{logon}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <WakeToRun>{"true" if spec.wake_to_run else "false"}</WakeToRun>
    <ExecutionTimeLimit>PT{int(spec.execution_limit_hours)}H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(spec.python)}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{escape(spec.repo)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    return xml


def xml_fingerprint(xml: str) -> str:
    """Hash of the parts that define behaviour (so a task edited by hand in the Task Scheduler is noticed)."""
    keep = re.sub(r"<StartBoundary>\d{4}-\d{2}-\d{2}T", "<StartBoundary>", xml)          # the creation date is not behaviour
    keep = re.sub(r"\s+", "", keep)
    return hashlib.sha256(keep.encode("utf-8")).hexdigest()[:16]


def _current_user() -> str:
    domain, name = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{name}" if domain else name


def check_name(phase: str) -> None:
    if phase not in TASKS or not NAME_RE.match(TASKS[phase]):
        raise ValueError("invalid task")


# ------------------------------------------------------------------------------------------- backends
class SchedulerError(RuntimeError):
    """The Task Scheduler refused or could not be reached (the message is safe to show)."""


LOGGED_OFF_REFUSED = ("Windows refused to create a task that runs while you are logged off ({detail}). That needs an administrator "
                      "or the \"Log on as a batch job\" right. The tasks were created to run while you are SIGNED IN instead, and "
                      "\"Run even when I am logged off\" has been switched off. To run when logged off, start the application once "
                      "with \"Run as administrator\" and tick it again.")


def explain_register_failure(message: str) -> str:
    """The Task Scheduler's own message plus what usually fixes a refusal (it is shown in the language of the PC's Windows)."""
    return (f"{message}. Windows may be refusing your account to create scheduled tasks. Close the application, start "
            "start_stockdata.bat with \"Run as administrator\" once, and press Install again; if your PC is managed by an "
            "organisation, its policy may forbid scheduled tasks (then use the Run now button, or ask your administrator). "
            "The task is only created for the StockDataMC folder and runs this application's own scripts.")


class SchedulerBackend:
    available = False
    name = "none"

    def register(self, path: str, xml: str) -> None: raise NotImplementedError
    def unregister(self, path: str) -> None: raise NotImplementedError
    def set_enabled(self, path: str, enabled: bool) -> None: raise NotImplementedError
    def run_now(self, path: str) -> None: raise NotImplementedError
    def info(self, path: str) -> dict | None: raise NotImplementedError


class UnavailableBackend(SchedulerBackend):
    """Used where there is no Windows Task Scheduler (Linux/macOS). XML can still be previewed."""
    name = "unavailable"

    def _no(self, *_):
        raise SchedulerError("Windows Task Scheduler is only available on Windows. The task definition can be previewed here "
                             "and registered from the Windows machine that runs the application.")

    register = unregister = set_enabled = run_now = _no

    def info(self, path: str) -> dict | None:
        return None


class WindowsBackend(SchedulerBackend):
    """``schtasks.exe`` for create/delete/enable/disable/run and PowerShell's ScheduledTasks module for status (its output is
    locale-independent; ``schtasks /V`` is translated into the Windows display language and cannot be parsed reliably)."""
    available = True
    name = "windows"

    def __init__(self, runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None):
        self._run = runner or self._default_runner

    @staticmethod
    def _default_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, timeout=60, check=False)         # fixed argument lists; no shell

    def _call(self, argv: list[str], what: str) -> subprocess.CompletedProcess:
        try:
            cp = self._run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SchedulerError(f"could not {what}: {type(exc).__name__}") from exc
        if cp.returncode != 0:
            text = _decode(cp.stderr or cp.stdout)[:400]
            raise SchedulerError(f"could not {what}: {text.strip() or 'exit code ' + str(cp.returncode)}")
        return cp

    def register(self, path: str, xml: str) -> None:
        _check_path(path)
        fd, tmp = tempfile.mkstemp(suffix=".xml", prefix="stockdata_task_")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(xml.encode("utf-16"))                                 # schtasks wants UTF-16 with a byte-order mark
            self._call(["schtasks.exe", "/Create", "/TN", path, "/XML", tmp, "/F"], "register the scheduled task")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def unregister(self, path: str) -> None:
        _check_path(path)
        self._call(["schtasks.exe", "/Delete", "/TN", path, "/F"], "remove the scheduled task")

    def set_enabled(self, path: str, enabled: bool) -> None:
        _check_path(path)
        self._call(["schtasks.exe", "/Change", "/TN", path, "/ENABLE" if enabled else "/DISABLE"],
                   "enable the scheduled task" if enabled else "disable the scheduled task")

    def run_now(self, path: str) -> None:
        _check_path(path)
        self._call(["schtasks.exe", "/Run", "/TN", path], "start the scheduled task")

    def info(self, path: str) -> dict | None:
        _check_path(path)
        folder, name = path.rsplit("\\", 1)
        script = ("$ErrorActionPreference='Stop'; try { "
                  f"$t = Get-ScheduledTask -TaskPath '{folder}\\' -TaskName '{name}'; $i = $t | Get-ScheduledTaskInfo; "
                  "function f($d) { if ($d -and $d.Year -gt 2000) { $d.ToUniversalTime().ToString('o') } else { $null } }; "
                  "[pscustomobject]@{exists=$true; state=[string]$t.State; enabled=[bool]$t.Settings.Enabled; last_run=(f $i.LastRunTime); "
                  "next_run=(f $i.NextRunTime); last_result=[int64]$i.LastTaskResult; missed=[int]$i.NumberOfMissedRuns} | ConvertTo-Json -Compress "
                  "} catch { '{\"exists\":false}' }")
        cp = self._call(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                        "read the scheduled task")
        try:
            data = json.loads(_decode(cp.stdout).strip() or "{}")
        except ValueError:
            raise SchedulerError("the Task Scheduler answered in an unexpected format") from None
        if not data.get("exists"):
            return None
        xml = None
        try:
            xml = _decode(self._call(["schtasks.exe", "/Query", "/TN", path, "/XML"], "read the task definition").stdout)
        except SchedulerError:
            pass
        data["xml"] = xml
        return data


def _decode(raw) -> str:
    if isinstance(raw, str):
        return raw
    raw = raw or b""
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    return raw.decode("utf-8", "replace")


def _check_path(path: str) -> None:
    folder, _, name = path.rpartition("\\")
    if folder != FOLDER or not NAME_RE.match(name):
        raise SchedulerError("refusing to touch a task outside the StockDataMC folder")


def get_backend() -> SchedulerBackend:
    return WindowsBackend() if sys.platform == "win32" else UnavailableBackend()


# --------------------------------------------------------------------------------------------- status
def describe_result(code: int | None) -> str | None:
    if code is None:
        return None
    if code in SCHEDULER_RESULTS:
        return SCHEDULER_RESULTS[code]
    if code in RUNNER_EXIT:
        return RUNNER_EXIT[code]
    return f"exit code {code}"


def task_status(backend: SchedulerBackend, spec: TaskSpec, *, expected_xml: str | None = None) -> dict:
    """What the Task Scheduler says about one task, as the interface shows it."""
    path = task_path(spec.phase)
    out = {"phase": spec.phase, "task": path, "backend": backend.name, "available": backend.available, "registered": False,
           "enabled": None, "state": None, "next_run": None, "last_run": None, "last_result": None, "last_result_text": None,
           "missed_runs": None, "definition_current": None, "problem": None}
    if not backend.available:
        out["problem"] = "Windows Task Scheduler is only available on Windows"
        return out
    try:
        info = backend.info(path)
    except SchedulerError as exc:
        out["problem"] = str(exc)
        return out
    if not info:
        return out
    out.update(registered=True, enabled=info.get("enabled"), state=info.get("state"), next_run=info.get("next_run"),
               last_run=info.get("last_run"), last_result=info.get("last_result"),
               last_result_text=describe_result(info.get("last_result")), missed_runs=info.get("missed"))
    if expected_xml and info.get("xml"):
        out["definition_current"] = xml_fingerprint(info["xml"]) == xml_fingerprint(expected_xml)
    return out


def next_runs(time_ist: str, weekdays: list[int], *, now: datetime | None = None, count: int = 3) -> list[str]:
    """The next scheduled instants in IST (informational; the Task Scheduler is the authority)."""
    now = (now or datetime.now(IST)).astimezone(IST)
    hh, mm = (int(x) for x in time_ist.split(":"))
    out, day = [], now.date()
    for _ in range(14):
        at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
        if at > now and day.weekday() in weekdays:
            out.append(at.isoformat(timespec="minutes"))
            if len(out) == count:
                break
        day += timedelta(days=1)
    return out
