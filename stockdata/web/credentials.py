"""Dhan credential handling for the web application.

* The access token and client id are NEVER returned by any API, logged, or put into an error message. The UI gets
  only: configured yes/no, where it comes from, a masked tail, and (for a JWT-shaped token) its expiry time.
* Updates go to the untracked ``.env`` file (git-ignored, owner-only permissions where the OS supports it) via an
  atomic write, and into this process's environment. Workers receive the credentials through their process
  environment, never on a command line.
* Values saved here take precedence over inherited environment variables for the workers this application starts.
  (The plain command line keeps its documented order: a real environment variable wins over ``.env``.)
* Every provider (Dhan, FYERS, Upstox) is handled the same way: the fields it needs come from its adapter's capabilities,
  only those keys can be written, and a worker receives ONLY the credentials of its own provider.
"""
from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from stockdata.providers import PROVIDER_NAMES, get_provider
from stockdata.providers.tokens import jwt_expiry as _jwt_expiry_shared
from stockdata.web.redact import register_secret
from stockdata.timeutil import IST

TOKEN_KEY, CLIENT_KEY = "DHAN_ACCESS_TOKEN", "DHAN_CLIENT_ID"
# Windows refuses to replace a file that another program has open (an editor, antivirus, a sync or backup tool) for a moment,
# so a refused replace is retried with a short back-off before anything else is tried.
REPLACE_RETRY_DELAYS = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0)


class CredentialFileError(OSError):
    """The credentials file could not be written. The message never contains a credential."""
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._\-+/=]{6,4096}$")


def _windows_account() -> str | None:
    """DOMAIN\\user of the account running this process (qualified, so icacls resolves it on domain and local accounts alike)."""
    user = os.environ.get("USERNAME")
    if not user:
        return None
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if key.strip() and value:
            out[key.strip()] = value
    return out


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    return "•" * 6 + value[-4:] if len(value) > 8 else "•" * 6


def _jwt_expiry(token: str | None) -> datetime | None:
    """Expiry (``exp`` claim) of a JWT-shaped token. Only the expiry is read; nothing else leaves this function."""
    return _jwt_expiry_shared(token)


def provider_keys(provider: str) -> list[str]:
    return [f.env_key for f in get_provider(provider).capabilities().auth.fields]


def all_provider_keys() -> set[str]:
    return {k for name in PROVIDER_NAMES for k in provider_keys(name)}


class CredentialStore:
    def __init__(self, env_file: Path, environ: dict | None = None):
        self.env_file = Path(env_file)
        self.environ = os.environ if environ is None else environ
        self._register()
        self._sleep = time.sleep

    def _file(self) -> dict[str, str]:
        return parse_env_file(self.env_file)

    def _register(self) -> None:
        for name in PROVIDER_NAMES:
            for v in self.provider_env(name).values():
                register_secret(v)

    def resolve(self) -> dict[str, str]:
        """INTERNAL: credentials for a worker's environment (file value first, then the process environment)."""
        f = self._file()
        out = {}
        for key in (TOKEN_KEY, CLIENT_KEY):
            value = f.get(key) or self.environ.get(key)
            if value:
                out[key] = value
        return out

    def status(self) -> dict:
        f, env = self._file(), self.environ

        def one(key: str) -> dict:
            if f.get(key):
                src, val = ".env file", f[key]
            elif env.get(key):
                src, val = "environment variable", env[key]
            else:
                return {"configured": False, "source": None, "masked": None}
            return {"configured": True, "source": src, "masked": _mask(val)}

        token, client = one(TOKEN_KEY), one(CLIENT_KEY)
        exp = _jwt_expiry(self.resolve().get(TOKEN_KEY))
        if exp:
            token["expires_at_utc"] = exp.isoformat(timespec="seconds")
            token["expires_at_ist"] = exp.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
            token["expired"] = exp <= datetime.now(timezone.utc)
            token["seconds_left"] = int((exp - datetime.now(timezone.utc)).total_seconds())
        token["expiry_known"] = exp is not None
        return {"access_token": token, "client_id": client, "env_file": str(self.env_file),
                "ready": token["configured"] and client["configured"]}

    # ---------------------------------------------------------------------------- any provider
    def _value(self, key: str) -> tuple[str | None, str | None]:
        f = self._file()
        if f.get(key):
            return f[key], ".env file"
        if self.environ.get(key):
            return self.environ[key], "environment variable"
        return None, None

    def provider_env(self, provider: str) -> dict[str, str]:
        """INTERNAL: the credentials of ONE provider, for the environment of a worker that talks to that provider."""
        out = {}
        for key in provider_keys(provider):
            value, _ = self._value(key)
            if value:
                out[key] = value
        return out

    def provider_status(self, provider: str) -> dict:
        """What the interface may know: per field configured / source / masked tail, plus what can be said about the token
        (state, expiry) without contacting the provider. Never a value."""
        adapter = get_provider(provider)
        fields = []
        for f in adapter.capabilities().auth.fields:
            value, src = self._value(f.env_key)
            fields.append({"env_key": f.env_key, "label": f.label, "secret": f.secret, "required": f.required,
                           "configured": bool(value), "source": src, "masked": _mask(value) if value else None})
        auth = adapter.credential_status(self.provider_env(provider))
        return {"provider": provider, "fields": fields, "auth": auth.to_dict(), "env_file": str(self.env_file)}

    def update_provider(self, provider: str, values: dict[str, str]) -> dict:
        allowed = {f.env_key: f for f in get_provider(provider).capabilities().auth.fields}
        changes = {}
        for key, value in values.items():
            if key not in allowed:
                raise ValueError(f"{key} is not a credential of {provider}")
            if value is None or value == "":
                continue
            value = value.strip()
            if not _SAFE_VALUE.match(value):
                raise ValueError(f"{allowed[key].label} has an invalid format (6-4096 letters, digits and . _ - + / = only; "
                                 "no spaces or quotes)")
            changes[key] = value
        if not changes:
            raise ValueError("nothing to update")
        self._write(changes)
        return self.provider_status(provider)

    def clear_provider(self, provider: str) -> dict:
        """Remove every stored credential of a provider from the .env file and this process (a deliberate, confirmed action)."""
        keys = set(provider_keys(provider))
        try:
            lines = self.env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in keys or ln.lstrip().startswith("#")]
        if kept != lines:
            self._atomic_write("\n".join(kept) + ("\n" if kept else ""))
        for k in keys:
            self.environ.pop(k, None)
        return self.provider_status(provider)

    def update(self, *, access_token: str | None = None, client_id: str | None = None) -> dict:
        changes = {}
        for key, value in ((TOKEN_KEY, access_token), (CLIENT_KEY, client_id)):
            if value is None or value == "":
                continue
            value = value.strip()
            if not _SAFE_VALUE.match(value):
                raise ValueError(f"{'access token' if key == TOKEN_KEY else 'client id'} has an invalid format "
                                 "(6-4096 letters, digits and . _ - + / = only; no spaces or quotes)")
            changes[key] = value
        if not changes:
            raise ValueError("nothing to update")
        self._write(changes)
        return self.status()

    def _write(self, changes: dict[str, str]) -> None:
        lines = []
        try:
            lines = self.env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            pass
        done = set()
        for i, raw in enumerate(lines):
            key = raw.split("=", 1)[0].strip()
            if key in changes and not raw.lstrip().startswith("#"):
                lines[i] = f"{key}={changes[key]}"
                done.add(key)
        lines += [f"{k}={v}" for k, v in changes.items() if k not in done]
        self._atomic_write(("\n".join(lines) + "\n"))
        for key, value in changes.items():
            self.environ[key] = value
            register_secret(value)

    def _atomic_write(self, text: str) -> None:
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.env_file.parent), prefix=".env.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            self._restrict(Path(tmp))
            self._replace(Path(tmp), self.env_file, text)
        finally:
            try:
                os.unlink(tmp)                       # gone already after a successful replace
            except OSError:
                pass
        self._restrict(self.env_file)

    def _replace(self, tmp: Path, dest: Path, text: str) -> None:
        """Move ``tmp`` over ``dest``. On Windows a refused replace is usually temporary (another program holds the file), a
        read-only flag, or damaged permissions: retry, repair access once, and as a last resort overwrite in place."""
        for attempt, delay in enumerate(REPLACE_RETRY_DELAYS):
            if delay:
                self._sleep(delay)
            try:
                os.replace(tmp, dest)
                return
            except PermissionError:
                if attempt == 1:
                    self._make_writable(dest)
        try:
            self._overwrite_in_place(dest, text)
        except OSError:
            where = f'"{dest}"'
            hint = (f" Close any program that has it open (an editor, antivirus or a sync/backup tool), or clear the read-only flag "
                    f"with:  attrib -R {where}  then save again." if sys.platform == "win32"
                    else " Check that nothing else has it open and that it is writable, then save again.")
            raise CredentialFileError(f"The credentials file {where} could not be replaced or written.{hint}") from None

    @staticmethod
    def _overwrite_in_place(dest: Path, text: str) -> None:
        """Last resort when the file cannot be replaced but can still be written: not atomic, so the old content is restored
        if the write fails half way."""
        try:
            before = dest.read_bytes()
        except FileNotFoundError:
            before = None
        try:
            with open(dest, "wb") as fh:
                fh.write(text.encode("utf-8"))
        except OSError:
            if before is not None:
                try:
                    dest.write_bytes(before)
                except OSError:
                    pass
            raise

    @staticmethod
    def _make_writable(path: Path) -> None:
        """Clear a read-only flag and, on Windows, give the current user full control again (best effort; never fatal)."""
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
        account = _windows_account()
        if account:
            try:
                subprocess.run(["icacls", str(path), "/grant:r", f"{account}:F"], capture_output=True, timeout=15, check=False)
            except Exception:
                pass

    @staticmethod
    def _restrict(path: Path) -> None:
        """Owner-only access: chmod 600 on POSIX; on Windows grant the current user full control FIRST and only then drop the
        inherited permissions, so a grant that fails can never leave the file with no usable permissions (best effort)."""
        try:
            if sys.platform == "win32":
                account = _windows_account()
                if account:
                    granted = subprocess.run(["icacls", str(path), "/grant:r", f"{account}:F"], capture_output=True, timeout=15, check=False)
                    if granted.returncode == 0:
                        subprocess.run(["icacls", str(path), "/inheritance:r"], capture_output=True, timeout=15, check=False)
            else:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
