"""Domain-level exceptions. No Telegram/Qt types leak through these."""

from __future__ import annotations


class TeloudeError(Exception):
    """Base class for all expected application errors."""


class TransferCancelled(TeloudeError):
    """The user explicitly cancelled the operation. NEVER treated as success."""


class TransientNetworkError(TeloudeError):
    """Connection-level failure: reconnect and retry the same part."""


class RateLimited(TeloudeError):
    """Telegram asked us to slow down (FLOOD_WAIT)."""

    def __init__(self, seconds: float):
        super().__init__(f"rate limited for {seconds:.0f}s")
        self.seconds = float(seconds)


class UploadSessionInvalid(TeloudeError):
    """Server no longer knows the partially uploaded file (expired/invalid file id)."""


class PermanentTransferError(TeloudeError):
    """Not retryable (file vanished, caption too long, permission denied...)."""


class TopicNotFound(TeloudeError):
    """The forum topic was deleted on Telegram's side."""


class RetriesExhausted(TeloudeError):
    """A part failed too many consecutive times. The checkpoint is kept so it can be resumed."""


class UnsafePathError(TeloudeError):
    """Remote-supplied path is not safe to materialise on the local disk."""


class NotAuthorized(TeloudeError):
    pass


class PasswordRequired(TeloudeError):
    """Two-step verification password is required."""


class InvalidCode(TeloudeError):
    pass


class InvalidPassword(TeloudeError):
    pass


class ProxyUnsupported(TeloudeError):
    """e.g. fake-TLS ('ee') MTProxy secrets, which Telethon cannot speak."""


class DatabaseCorruptError(TeloudeError):
    pass


class MissingApiCredentials(TeloudeError):
    pass
