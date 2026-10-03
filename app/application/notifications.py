"""Turns a finished run into at most ONE user notification. Cancelled runs never look like success."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..domain.models import RunOutcome
from ..infrastructure.repositories import RunRepo
from .backup import RunSummary


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    kind: str  # success | warning | error


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def describe(s: RunSummary) -> Notification | None:
    noun = "Backup" if s.kind == "backup" else "Restore"
    done = s.succeeded
    if s.outcome == RunOutcome.COMPLETED:
        extra = f", {s.skipped} skipped" if s.skipped else ""
        return Notification(f"{noun} completed", f"{done} file(s), {human_size(s.bytes_done)}{extra}.", "success")
    if s.outcome == RunOutcome.PARTIAL:
        return Notification(
            f"{noun} partially completed", f"{done} of {s.total} file(s) succeeded; {s.failed} failed.", "warning"
        )
    if s.outcome == RunOutcome.FAILED:
        reason = s.errors[0] if s.errors else "unknown error"
        return Notification(f"{noun} failed", reason[:200], "error")
    return None  # cancelled / nothing to do / still running -> no notification


class NotificationService:
    def __init__(self, runs: RunRepo, sink: Callable[[Notification], None], enabled: Callable[[], bool] = lambda: True):
        self._runs, self._sink, self._enabled = runs, sink, enabled

    def notify_run(self, s: RunSummary) -> bool:
        note = describe(s)
        if note is None:
            return False
        if not self._runs.claim_notification(s.run_id):  # atomic: second caller gets False
            return False
        if self._enabled():
            self._sink(note)
        return True
