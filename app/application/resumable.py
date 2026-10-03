""""What can still be resumed?" - the read-only view the UI uses after a failed run.

A failed backup must never become a dead end. This service answers, from persisted state only:

* which files still have work left,
* how many bytes Telegram has already acknowledged for them (the real checkpoint value),
* how big the remaining job is,
* and why each one stopped.

It performs no network I/O and never mutates anything, so it is safe to call from the Qt thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import FileState, TransferState
from ..infrastructure.repositories import Repos

__all__ = ["ResumableItem", "ResumeSummary", "ResumeService"]

#: Transfer states a Resume may continue. ``PERMANENT_FAILED`` is intentionally absent.
RESUMABLE_STATES: frozenset[TransferState] = frozenset(
    {
        TransferState.INTERRUPTED,
        TransferState.FAILED,
        TransferState.RECOVERABLE_FAILED,
        TransferState.CANCELLED,
    }
)

#: File states that still count as "needs uploading".
WAITING_STATES: frozenset[FileState] = frozenset(
    {FileState.PENDING, FileState.UPLOADING, FileState.FAILED}
)


@dataclass
class ResumableItem:
    file_id: int
    transfer_id: int
    rel_path: str
    size: int
    done_bytes: int  # bytes Telegram acknowledged AND that were checkpointed
    state: str
    error: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.size - self.done_bytes)

    @property
    def started(self) -> bool:
        return self.done_bytes > 0


@dataclass
class ResumeSummary:
    kind: str = "backup"
    storage_id: int | None = None
    local_root: str | None = None
    items: list[ResumableItem] = field(default_factory=list)
    not_started: int = 0  # files waiting that never had a single part acknowledged
    not_started_bytes: int = 0
    blocked: int = 0  # permanently failed: needs a human, Resume will not touch them

    @property
    def count(self) -> int:
        return len(self.items) + self.not_started

    @property
    def total_bytes(self) -> int:
        return sum(i.size for i in self.items) + self.not_started_bytes

    @property
    def done_bytes(self) -> int:
        return sum(i.done_bytes for i in self.items)

    @property
    def resumable(self) -> bool:
        return self.count > 0

    @property
    def reason(self) -> str:
        for i in self.items:
            if i.error:
                return i.error
        return ""


class ResumeService:
    def __init__(self, repos: Repos):
        self.repos = repos

    def summary(self, storage_id: int, kind: str = "backup") -> ResumeSummary:
        files = self.repos.files
        out = ResumeSummary(kind=kind, storage_id=storage_id)
        row = self.repos.storages.get(storage_id) if storage_id is not None else None
        out.local_root = (row["local_root"] if row else None) or None

        seen: set[int] = set()
        for t in self.repos.transfers.recoverable_for_storage(storage_id):
            if t["kind"] != kind or t["file_id"] is None:
                continue
            seen.add(int(t["file_id"]))
            done = int(t["done_bytes"] or 0)
            out.items.append(
                ResumableItem(
                    file_id=int(t["file_id"]),
                    transfer_id=int(t["id"]),
                    rel_path=str(t["rel_path"]),
                    size=int(t["total_bytes"] or 0) or int(t["src_size"] or 0),
                    done_bytes=min(done, int(t["total_bytes"] or 0) or done),
                    state=str(t["state"]),
                    error=str(t["error"] or ""),
                )
            )
        # Files that are still waiting but never produced a checkpoint (e.g. the run stopped early).
        for f in files.current_index(storage_id).values():
            fid = int(f["id"])
            if fid in seen:
                continue
            if FileState(f["state"]) in WAITING_STATES:
                out.not_started += 1
                out.not_started_bytes += int(f["size"] or 0)
            elif FileState(f["state"]) == FileState.PERMANENT_FAILED:
                out.blocked += 1
        return out
