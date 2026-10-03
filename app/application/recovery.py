"""Crash / power-loss recovery, run once at startup before anything else touches transfers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..domain.models import FileState
from ..infrastructure.repositories import Repos
from .scanner import _long

log = logging.getLogger("teloude.recovery")


@dataclass
class RecoveryReport:
    interrupted: int = 0
    discarded_checkpoints: int = 0
    files_reset: int = 0
    runs_abandoned: int = 0


class RecoveryService:
    def __init__(self, repos: Repos):
        self.repos = repos

    def recover(self) -> RecoveryReport:
        """Nothing that was merely *started* is assumed to have succeeded.

        * transfers that claimed to be active become INTERRUPTED (resumable)
        * files stuck in UPLOADING return to PENDING (never BACKED_UP)
        * checkpoints whose source file changed are discarded
        * checkpoints for a volume that was being finalised are kept: the next run first asks Telegram
          whether that message already exists (adopt instead of duplicate)
        """
        rep = RecoveryReport()
        rep.runs_abandoned = self.repos.runs.abandon_running()
        interrupted = self.repos.transfers.mark_all_active_interrupted()
        rep.interrupted = len(interrupted)
        rep.files_reset = self.repos.files.reset_stuck_uploading()
        for t in interrupted:
            if t["kind"] != "backup" or t["file_id"] is None:
                continue
            f = self.repos.files.get(t["file_id"])
            valid = False
            if f is not None and f["local_path"] and f["state"] != FileState.BACKED_UP:
                try:
                    st = os.stat(_long(f["local_path"]))
                    valid = st.st_size == t["src_size"] and st.st_mtime_ns == t["src_mtime_ns"]
                except OSError:
                    valid = False
            if not valid:
                self.repos.transfers.db.execute("DELETE FROM transfers WHERE id=?", (t["id"],))
                rep.discarded_checkpoints += 1
        if rep.interrupted or rep.runs_abandoned:
            log.info("recovery finished", extra={"data": rep.__dict__})
        return rep
