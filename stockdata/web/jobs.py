"""Job definitions: validation, the exact engine command line, duplicate fingerprints and exit-code mapping.

A job is never free text: only the allow-listed fields below can reach the engine command line, and the command is
always ``python -m stockdata download|instruments ...`` (the existing CLI). Values are passed as separate argv items
(never through a shell)."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_EXPLICIT_INSTRUMENTS = 1000          # keeps the command line far below the Windows 32,767-character limit
SYMBOL_RE = re.compile(r"^[A-Za-z0-9&._\-]{1,32}$")
ERROR_CODE_RE = re.compile(r"^DH-\d{3}$")
EARLIEST_DATE = date(1990, 1, 1)


class Selection(BaseModel):
    mode: Literal["all", "symbols", "security_ids", "group"] = "all"
    symbols: list[str] = Field(default_factory=list)
    security_ids: list[int] = Field(default_factory=list)
    group_id: int | None = None
    limit: int | None = Field(default=None, ge=1, le=100_000)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = s.strip().upper()
            if not SYMBOL_RE.match(s):
                raise ValueError(f"invalid symbol {s!r}")
            if s not in out:
                out.append(s)
        if len(out) > MAX_EXPLICIT_INSTRUMENTS:
            raise ValueError(f"at most {MAX_EXPLICIT_INSTRUMENTS} symbols; use the whole universe instead")
        return out

    @field_validator("security_ids")
    @classmethod
    def _ids(cls, v: list[int]) -> list[int]:
        if any(i <= 0 for i in v):
            raise ValueError("security ids must be positive integers")
        out = list(dict.fromkeys(v))
        if len(out) > MAX_EXPLICIT_INSTRUMENTS:
            raise ValueError(f"at most {MAX_EXPLICIT_INSTRUMENTS} instruments; use the whole universe instead")
        return out

    @model_validator(mode="after")
    def _consistent(self):
        if self.mode == "symbols" and not self.symbols:
            raise ValueError("select at least one symbol")
        if self.mode == "security_ids" and not self.security_ids:
            raise ValueError("select at least one instrument")
        if self.mode == "group" and self.group_id is None:
            raise ValueError("choose a saved group")
        return self


class JobSpec(BaseModel):
    kind: Literal["download", "instruments_load"] = "download"
    timeframe: Literal["daily", "15min"] | None = None
    name: str | None = Field(default=None, max_length=120)
    selection: Selection | None = None
    start_date: date | None = None
    end_date: date | None = None
    retry_failed: bool = False
    empty_error_codes: list[str] = Field(default_factory=list)
    confirm_full: bool = False
    start_after: datetime | None = Field(default=None, description="do not start before this time (a naive value is read as IST)")

    @field_validator("start_after")
    @classmethod
    def _start_after(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return v.astimezone(timezone.utc)

    @field_validator("empty_error_codes")
    @classmethod
    def _codes(cls, v: list[str]) -> list[str]:
        for c in v:
            if not ERROR_CODE_RE.match(c):
                raise ValueError(f"invalid error code {c!r} (expected e.g. DH-907)")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        if self.kind == "download":
            if self.timeframe is None:
                raise ValueError("timeframe is required for a download")
            if self.selection is None:
                raise ValueError("a selection is required")
            if self.selection.mode == "all" and not self.confirm_full:
                raise ValueError("a whole-universe download needs explicit confirmation (confirm_full)")
            if self.empty_error_codes and not self.retry_failed:
                raise ValueError("empty_error_codes only applies together with retry_failed")
            if self.empty_error_codes and self.selection.mode == "all":
                raise ValueError("recording failures as 'empty' must name reviewed instruments, never the whole universe")
        if self.start_date and self.start_date < EARLIEST_DATE:
            raise ValueError("start date is before 1990-01-01")
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValueError("start date must be before the (exclusive) end date")
        return self


def canonical(spec: JobSpec, security_ids: list[int] | None = None) -> dict:
    """Normalised content of a job (used for the duplicate fingerprint and stored with the job)."""
    sel = spec.selection
    return {
        "kind": spec.kind, "timeframe": spec.timeframe,
        "selection": None if sel is None else {
            "mode": sel.mode, "limit": sel.limit,
            "symbols": sorted(sel.symbols) if sel.mode == "symbols" else [],
            "security_ids": sorted(security_ids if security_ids is not None else sel.security_ids)
            if sel.mode in ("security_ids", "group") else []},
        "start_date": spec.start_date.isoformat() if spec.start_date else None,
        "end_date": spec.end_date.isoformat() if spec.end_date else None,
        "retry_failed": spec.retry_failed, "empty_error_codes": sorted(spec.empty_error_codes),
    }


def fingerprint(spec: JobSpec, security_ids: list[int] | None = None) -> str:
    return hashlib.sha1(json.dumps(canonical(spec, security_ids), sort_keys=True).encode()).hexdigest()[:16]


def default_name(spec: JobSpec, security_ids: list[int] | None = None) -> str:
    if spec.kind == "instruments_load":
        return "Refresh instrument master"
    sel = spec.selection
    tf = "Daily" if spec.timeframe == "daily" else "15-minute"
    if sel.mode == "all":
        what = "full universe"
    elif sel.mode == "symbols":
        what = ", ".join(sel.symbols[:3]) + (f" +{len(sel.symbols) - 3}" if len(sel.symbols) > 3 else "")
    else:
        n = len(security_ids if security_ids is not None else sel.security_ids)
        what = f"{n} instrument{'s' if n != 1 else ''}"
    extra = " (retry failed)" if spec.retry_failed else ""
    return f"{tf} - {what}{extra}"


def engine_args(spec: JobSpec, *, data_dir: str, security_ids: list[int] | None = None,
                rps: float | None = None, max_per_run: int | None = None, max_per_day: int | None = None) -> list[str]:
    """Arguments after ``python -m stockdata`` - the same command a person would type."""
    args = ["--data-dir", data_dir]
    if spec.kind == "instruments_load":
        return args + ["instruments", "load", "--fetch"]
    sel = spec.selection
    args += ["download", spec.timeframe]
    if sel.mode == "all":
        args += ["--all", "--confirm-full-download"]
    elif sel.mode == "symbols":
        args += ["--symbols", ",".join(sel.symbols)]
    else:
        ids = security_ids if security_ids is not None else sel.security_ids
        args += ["--security-ids", ",".join(str(i) for i in ids)]
    if sel.limit:
        args += ["--limit", str(sel.limit)]
    if spec.start_date:
        args += ["--start-date", spec.start_date.isoformat()]
    if spec.end_date:
        args += ["--end-date", spec.end_date.isoformat()]
    if spec.retry_failed:
        args.append("--retry-failed")
    if spec.empty_error_codes:
        args += ["--empty-error-codes", ",".join(spec.empty_error_codes)]
    if rps:
        args += ["--rps", f"{rps:g}"]
    if max_per_run:
        args += ["--max-requests-per-run", str(max_per_run)]
    if max_per_day:
        args += ["--max-requests-per-day", str(max_per_day)]
    return args


def command_text(args: list[str]) -> str:
    """The equivalent command line, for display (quoted the way the local shell expects)."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(["python", "-m", "stockdata", *args])
    return "python -m stockdata " + " ".join(shlex.quote(a) for a in args)


# Engine exit codes (see README / docs/RUNBOOK.md): 0 completed, 2 completed with failed/retriable windows,
# 3 aborted (auth, API down), 4 stopped at a request limit, 5 stopped on request, 130 interrupted.
def classify_exit(code: int | None, *, pending_action: str | None, log_text: str) -> tuple[str, str, str | None]:
    """-> (job status, engine status, next-action hint)."""
    lowered = log_text.lower()
    if code == 0:
        return "completed", "completed", None
    if code == 2:
        return "completed_with_warnings", "completed_with_errors", "Review failed or retriable windows in Data Quality; retry them from the job."
    if code == 4:
        return "stopped_by_limit", "stopped_request_limit", "Wait for the request budget (IST midnight resets the day count) or raise the limit deliberately, then Resume."
    if code == 5:
        if pending_action == "stop":
            return "stopped", "stopped_by_user", "Resume when ready; completed windows are kept and will not be requested again."
        return "paused", "stopped_by_user", "Resume when ready; completed windows are kept and will not be requested again."
    if code == 130:
        return "interrupted", "aborted", "Resume: the same command continues from the last committed window."
    if code == 3:
        if "authentication" in lowered or "token" in lowered or "dh-901" in lowered:
            return "failed", "aborted", "The access token was rejected or has expired. Update it in Settings, then Resume."
        return "failed", "aborted", "The run aborted (see the log). Fix the cause, then Resume."
    if "could not set lock" in lowered or "conflicting lock" in lowered or "being used by another process" in lowered:
        return "failed", "error", "The database file is locked by another process. Close it, then Resume."
    return "failed", "error", "The worker exited with an error (see the log). Fix the cause, then Resume."
