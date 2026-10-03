"""Plain domain models and enums (no I/O, no Qt, no Telegram)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB


class TransferState(StrEnum):
    QUEUED = "queued"
    HASHING = "hashing"
    UPLOADING = "uploading"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    RECONNECTING = "reconnecting"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"  # legacy/undifferentiated failure; treated as recoverable
    RECOVERABLE_FAILED = "recoverable_failed"  # temporary: network, proxy, timeout, lost session...
    PERMANENT_FAILED = "permanent_failed"  # needs a human: auth, permission, bad destination...
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"  # persisted after crash/shutdown; resumable

    @property
    def is_active(self) -> bool:
        return self in {
            TransferState.QUEUED,
            TransferState.HASHING,
            TransferState.UPLOADING,
            TransferState.DOWNLOADING,
            TransferState.PAUSED,
            TransferState.RECONNECTING,
            TransferState.RETRYING,
        }

    @property
    def is_final(self) -> bool:
        return self in {
            TransferState.COMPLETED,
            TransferState.FAILED,
            TransferState.RECOVERABLE_FAILED,
            TransferState.PERMANENT_FAILED,
            TransferState.CANCELLED,
        }

    @property
    def is_failed(self) -> bool:
        return self in {TransferState.FAILED, TransferState.RECOVERABLE_FAILED, TransferState.PERMANENT_FAILED}

    @property
    def is_recoverable(self) -> bool:
        """``True`` when a later run may safely continue this transfer from its checkpoint.

        A *cancelled* transfer is still resumed on request: the user asked to stop, not to throw the
        uploaded parts away. Only a permanent failure (and a completed transfer) is truly closed.
        """
        return self in {
            TransferState.FAILED,
            TransferState.RECOVERABLE_FAILED,
            TransferState.CANCELLED,
            TransferState.INTERRUPTED,
        }


class FileState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    BACKED_UP = "backed_up"  # only set after every volume was acknowledged by Telegram
    FAILED = "failed"  # temporary/recoverable: retried automatically on the next run
    PERMANENT_FAILED = "permanent_failed"  # needs a human; only retried once the source changes again
    SUPERSEDED = "superseded"  # an older version; its Telegram messages still exist
    DUPLICATE_SKIPPED = "duplicate_skipped"  # user chose Skip: identical content is already backed up
    REMOTE_DELETED = "remote_deleted"


class RunKind(StrEnum):
    BACKUP = "backup"
    RESTORE = "restore"


class RunOutcome(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOTHING_TO_DO = "nothing_to_do"


class DuplicateAction(StrEnum):
    SKIP = "skip"
    UPLOAD_AGAIN = "upload_again"
    CANCEL = "cancel"


class ConflictAction(StrEnum):
    SKIP = "skip"
    REPLACE = "replace"
    KEEP_BOTH = "keep_both"
    CANCEL = "cancel"


class ProxyState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass(frozen=True)
class ProxySettings:
    host: str = ""
    port: int = 443
    secret: str = ""  # hex string; NEVER logged
    enabled: bool = False

    def is_complete(self) -> bool:
        return bool(self.host.strip()) and 0 < self.port < 65536 and bool(self.secret.strip())

    def __repr__(self) -> str:  # never leak the secret through repr()/logging
        return f"ProxySettings(host={self.host!r}, port={self.port}, secret=<redacted>, enabled={self.enabled})"


@dataclass(frozen=True)
class SpeedLimit:
    """Upload/download limit in bytes per second. ``None`` means unlimited."""

    bytes_per_second: float | None = None

    @staticmethod
    def unlimited() -> SpeedLimit:
        return SpeedLimit(None)

    @staticmethod
    def mb(per_second: float) -> SpeedLimit:
        return SpeedLimit(per_second * MIB)


SPEED_PRESETS: dict[str, float | None] = {
    "Unlimited": None,
    "10 MB/s": 10 * MIB,
    "5 MB/s": 5 * MIB,
    "2 MB/s": 2 * MIB,
}


@dataclass(frozen=True)
class UploadLimits:
    """Telegram upload limits, discovered dynamically from the account/server."""

    part_size: int = 512 * KIB
    max_parts: int = 4000
    caption_limit: int = 1024
    premium: bool = False

    @property
    def max_volume_size(self) -> int:
        return self.part_size * self.max_parts


@dataclass
class ProgressSnapshot:
    done: int
    total: int
    speed: float  # bytes/s over a short sliding window
    eta: float | None  # seconds, None if unknown
    elapsed: float = 0.0

    @property
    def percent(self) -> float:
        return 100.0 if self.total <= 0 else min(100.0, self.done * 100.0 / self.total)


@dataclass
class ScannedFile:
    rel_path: str  # posix, includes top-level root folder name
    abs_path: str
    size: int
    mtime_ns: int

    @property
    def name(self) -> str:
        return self.rel_path.rsplit("/", 1)[-1]

    @property
    def ext(self) -> str:
        n = self.name
        return n.rsplit(".", 1)[-1].lower() if "." in n.strip(".") else ""


@dataclass
class ScanResult:
    new: list[ScannedFile] = field(default_factory=list)
    changed: list[ScannedFile] = field(default_factory=list)
    unchanged: list[ScannedFile] = field(default_factory=list)
    missing_locally: list[str] = field(default_factory=list)  # informational only; NEVER acted on
    blocked: list[str] = field(default_factory=list)  # permanently failed: needs a human, NOT retried
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def pending(self) -> list[ScannedFile]:
        return self.new + self.changed
