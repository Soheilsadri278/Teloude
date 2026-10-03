"""Cooperative pause / resume / cancel shared by every worker of a run."""

from __future__ import annotations

import asyncio
import threading

from ..domain.errors import TransferCancelled


class TransferControl:
    def __init__(self) -> None:
        self._run = asyncio.Event()
        self._run.set()
        self._cancelled = threading.Event()  # readable from worker threads (hashing, scanning)

    @property
    def paused(self) -> bool:
        return not self._run.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._run.set()  # wake anything parked on a pause so it can observe the cancellation

    def raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise TransferCancelled("cancelled by user")

    async def wait_ready(self) -> None:
        """Gate before starting *new* work. In-flight work is never interrupted by pause."""
        self.raise_if_cancelled()
        await self._run.wait()
        self.raise_if_cancelled()

    def stop_requested(self) -> bool:  # for thread-side polling
        return self._cancelled.is_set()
