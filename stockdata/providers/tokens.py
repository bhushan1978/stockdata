"""Credential helpers shared by every provider. Only a token's *expiry* is ever read out of it; nothing else leaves here."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone


def jwt_expiry(token: str | None) -> datetime | None:
    """Expiry (``exp`` claim, UTC) of a JWT-shaped token, or None when the token is not a JWT or has no expiry."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None
    except Exception:
        return None


def mask(value: str | None) -> str | None:
    if not value:
        return None
    return "•" * 6 + value[-4:] if len(value) > 8 else "•" * 6
