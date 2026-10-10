"""Secret redaction for everything the web application shows, logs or returns.

Two layers: exact values of the configured credentials (when known) and shape-based patterns for tokens,
authorization headers and client ids. Applied to log text, event messages, error messages and audit details."""
from __future__ import annotations

import re
import threading

REPLACEMENT = "[REDACTED]"

_PATTERNS = [
    # JWT-shaped tokens (Dhan access tokens are JWTs)
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"),
    # header / key=value forms
    re.compile(r"(?i)(access[-_ ]?token|authorization|x-api-key|api[-_ ]?key|secret|password|passwd)(['\"]?\s*[:=]\s*['\"]?)(bearer\s+)?[^\s'\",;&}]+"),
    re.compile(r"(?i)(dhan[-_ ]?client[-_ ]?id|client[-_ ]?id|dhanClientId)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",;&}]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
]

_lock = threading.Lock()
_known: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember an exact secret value so it is scrubbed wherever it appears."""
    if value and len(value) >= 6:
        with _lock:
            _known.add(value)


def forget_secrets() -> None:
    with _lock:
        _known.clear()


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    with _lock:
        known = sorted(_known, key=len, reverse=True)
    for secret in known:
        text = text.replace(secret, REPLACEMENT)

    def sub(m: re.Match) -> str:
        if m.re is _PATTERNS[0] or m.re is _PATTERNS[3]:
            return REPLACEMENT
        return f"{m.group(1)}{m.group(2)}{REPLACEMENT}"

    for pat in _PATTERNS:
        text = pat.sub(sub, text)
    return text


def redact_obj(obj):
    """Recursively redact strings in JSON-like structures (for error and audit payloads)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if re.search(r"(?i)token|secret|password|authorization|client_?id", str(k)) and v else redact_obj(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
