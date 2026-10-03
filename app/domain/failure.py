"""Classifying a failure as *recoverable* or *permanent*.

Teloude must never collapse every failure into one terminal state. A lost Wi-Fi connection must
leave a backup that the user can continue, while an invalid destination must not be retried in a
loop. This module is the single place where that decision is made; it is pure (no I/O, no Qt, no
Telegram) so it can be unit-tested exhaustively.

Rule of thumb
-------------
* **recoverable** - the *transport* or a *transient server condition* failed. Retrying the very same
  operation later can succeed without anybody changing anything.
* **permanent** - the *request itself* or the *local state* is wrong. Retrying can only succeed
  after a human changes something (re-authenticate, fix permissions, pick another folder, ...).
"""

from __future__ import annotations

from enum import StrEnum

from .errors import (
    InvalidCode,
    InvalidPassword,
    MissingApiCredentials,
    PasswordRequired,
    PermanentTransferError,
    ProxyUnsupported,
    RateLimited,
    RetriesExhausted,
    TeloudeError,
    TopicNotFound,
    TransferCancelled,
    TransientNetworkError,
    UnsafePathError,
    UploadSessionInvalid,
)

__all__ = ["FailureKind", "RECOVERABLE", "PERMANENT", "classify", "is_recoverable", "explain"]


class FailureKind(StrEnum):
    RECOVERABLE = "recoverable"
    PERMANENT = "permanent"


#: Exception types that are always recoverable (the same bytes can be sent again later).
RECOVERABLE: tuple[type[BaseException], ...] = (
    TransientNetworkError,
    UploadSessionInvalid,
    RateLimited,
    RetriesExhausted,  # the checkpoint is kept precisely so this can be resumed
    ConnectionError,
    TimeoutError,
    OSError,  # sockets, files locked by another process, disk full, ...
)

#: Exception types that need a human. Retrying them unchanged can never succeed.
PERMANENT: tuple[type[BaseException], ...] = (
    PermanentTransferError,
    UnsafePathError,
    MissingApiCredentials,
    InvalidCode,
    InvalidPassword,
    PasswordRequired,
    ProxyUnsupported,
    TopicNotFound,
    ValueError,
    TypeError,
    KeyError,
    PermissionError,
)


def classify(exc: BaseException) -> FailureKind:
    """Return ``FailureKind.RECOVERABLE`` or ``FailureKind.PERMANENT`` for ``exc``."""
    if isinstance(exc, TransferCancelled):
        # Cancellation is neither: callers keep it as its own state, never as "failed".
        return FailureKind.RECOVERABLE
    if isinstance(exc, PERMANENT):
        return FailureKind.PERMANENT
    if isinstance(exc, RECOVERABLE):
        return FailureKind.RECOVERABLE
    if isinstance(exc, TeloudeError):
        # A *new*, unclassified domain error defaults to permanent: we never silently retry
        # a condition nobody has reasoned about.
        return FailureKind.PERMANENT
    return FailureKind.PERMANENT


def is_recoverable(exc: BaseException) -> bool:
    return classify(exc) == FailureKind.RECOVERABLE


def explain(exc: BaseException) -> str:
    """Short, human, secret-free reason shown next to the Resume button."""
    if is_recoverable(exc):
        if isinstance(exc, UploadSessionInvalid):
            return "The upload session expired. Resuming starts a fresh one."
        if isinstance(exc, RateLimited):
            return "Telegram asked us to slow down."
        if isinstance(exc, RetriesExhausted):
            return "Telegram could not be reached."
        return "Connection lost."
    return "This needs your attention."
