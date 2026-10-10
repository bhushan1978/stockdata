"""Provider-independent error taxonomy. It decides how a window is recorded, exactly as the Dhan-specific taxonomy
always did (and Dhan's own exceptions are subclasses of these, so existing behaviour is unchanged):

  AuthError       the credential was rejected / expired      -> abort the whole run, record nothing
  TransientError  network, rate limit, 5xx                   -> retried by the client; if exhausted the window is 'retriable'
  PermanentError  the request itself was rejected            -> the window is 'failed' (never retried automatically)

Unknown provider error codes are always permanent for the window and are NEVER interpreted as "no data".
"""
from __future__ import annotations


class ProviderError(Exception):
    def __init__(self, message: str, *, http_status: int | None = None, error_code: str | None = None, attempts: int = 0):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.error_code = error_code
        self.attempts = attempts


class AuthError(ProviderError):
    pass


class PermanentError(ProviderError):
    pass


class TransientError(ProviderError):
    def __init__(self, *a, retry_after: float | None = None, rate_limited: bool = False, **kw):
        super().__init__(*a, **kw)
        self.retry_after = retry_after
        self.rate_limited = rate_limited


class ProviderSetupError(RuntimeError):
    """A problem the user must fix before any request can be made (missing credential, unsupported timeframe, unmappable
    instrument). The message is safe to show."""
