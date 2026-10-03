"""Pipelined, resumable, rate-limited upload of one *volume* (<= Telegram's max parts).

Key properties (each is covered by tests in ``tests/transfer``):

* **Bounded concurrency** - ``concurrency`` worker coroutines pull part indexes from a shared iterator.
  At most ``concurrency`` parts (each ``part_size`` bytes) are in memory/in flight at once; no unbounded
  task creation. Parts are written to Telegram out of order (allowed: each carries its index), while
  *progress* and the persisted *checkpoint* only count parts Telegram acknowledged.
* **No sequential stop-and-wait** - workers do not wait for each other. The speed limiter reserves
  tokens and sleeps only for its own deficit (``TokenBucket``), so throttling does not serialise.
* **Non-blocking observers** - progress callbacks and checkpoint writes run in a separate task and
  in worker threads, fed by coalesced snapshots. A slow UI/DB can never stall the upload path.
* **Resume, not restart** - completed part indexes are checkpointed. After a crash/pause/failure,
  only missing parts are re-sent into the *same* server-side upload session. If Telegram no longer
  knows the session (``UploadSessionInvalid``) a fresh session is started - the only case where bytes
  are re-sent.
* **Scoped retries** - one ``Backoff`` per part, a single-flight reconnect shared by all workers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..domain.errors import (
    PermanentTransferError,
    RateLimited,
    RetriesExhausted,
    TransferCancelled,
    TransientNetworkError,
    UploadSessionInvalid,
)
from ..domain.models import ProgressSnapshot, TransferState
from ..domain.planning import RangeSet, VolumePlan
from ..domain.progress import ProgressTracker
from ..domain.ratelimit import TokenBucket
from ..domain.retry import Backoff, RetryPolicy
from .control import TransferControl
from .diagnostics import TransferDiagnostics
from .ports import TelegramGateway, UploadSession
from .scanner import _long

log = logging.getLogger("teloude.pipeline")

SleepFn = Callable[[float], Awaitable[None]]


class CheckpointSink(Protocol):
    def save(self, tg_file_id: int, completed_parts: str, done_bytes: int) -> None:
        """Blocking persistence; always invoked from a worker thread."""


@dataclass
class PipelineConfig:
    concurrency: int = 4
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    progress_interval: float = 0.25
    checkpoint_interval: float = 1.0
    max_session_restarts: int = 2
    sleep: SleepFn = asyncio.sleep
    flood_wait_cap: float = 15 * 60


def _read_part(path: str, offset: int, length: int) -> bytes:
    with open(_long(path), "rb") as fh:
        fh.seek(offset)
        data = fh.read(length)
    if len(data) != length:
        raise PermanentTransferError("source file shrank or changed while uploading")
    return data


class UploadPipeline:
    def __init__(
        self,
        gateway: TelegramGateway,
        limiter: TokenBucket,
        control: TransferControl,
        config: PipelineConfig | None = None,
        diagnostics: TransferDiagnostics | None = None,
        on_state: Callable[[TransferState], None] | None = None,
    ):
        self.gateway = gateway
        self.limiter = limiter
        self.control = control
        self.config = config or PipelineConfig()
        self.diag = diagnostics or TransferDiagnostics()
        self._on_state = on_state
        self._state: TransferState | None = None
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_gen = 0

    # -- state ------------------------------------------------------------------------------
    def set_state(self, state: TransferState) -> None:
        if state != self._state:
            self._state = state
            if self._on_state:
                try:
                    self._on_state(state)
                except Exception:  # an observer must never break the transfer
                    log.exception("state observer failed")

    async def _sleep(self, seconds: float) -> None:
        """Cancel-aware sleep (wakes quickly if the user cancels)."""
        end = seconds
        step = 0.25
        while end > 0:
            self.control.raise_if_cancelled()
            chunk = min(step, end)
            await self.config.sleep(chunk)
            end -= chunk

    # -- reconnect (single-flight) ---------------------------------------------------------
    async def _reconnect(self, seen_gen: int) -> None:
        async with self._reconnect_lock:
            if self._reconnect_gen != seen_gen:
                return  # another worker already reconnected after our failure
            try:
                await self.gateway.reconnect()
            except (TimeoutError, TransientNetworkError, OSError):
                pass  # still offline: the part retry loop backs off and tries again
            else:
                self.diag.reconnects += 1
            self._reconnect_gen += 1

    # -- one part ----------------------------------------------------------------------------
    async def _send_part(self, session: UploadSession, idx: int, total: int, data: bytes) -> None:
        backoff = Backoff(self.config.retry)  # scoped to THIS part: cannot leak into later failures
        while True:
            self.control.raise_if_cancelled()
            await self.limiter.acquire(len(data))
            gen = self._reconnect_gen
            self.diag.upload_requests += 1
            self.diag.inflight += 1
            self.diag.max_inflight = max(self.diag.max_inflight, self.diag.inflight)
            try:
                await self.gateway.upload_part(session, idx, total, data)
                return
            except (TransferCancelled, UploadSessionInvalid, PermanentTransferError):
                raise
            except RateLimited as exc:
                self.diag.flood_waits += 1
                self.set_state(TransferState.RETRYING)
                await self._sleep(min(exc.seconds + 0.5, self.config.flood_wait_cap))
            except (TransientNetworkError, ConnectionError, TimeoutError, OSError) as exc:
                self.diag.retries += 1
                delay = backoff.next_delay()
                if backoff.attempts > backoff.policy.max_attempts:
                    raise RetriesExhausted(
                        f"part {idx} failed {backoff.attempts} consecutive times: {type(exc).__name__}"
                    ) from exc
                self.set_state(TransferState.RECONNECTING)
                await self._reconnect(gen)
                self.set_state(TransferState.RETRYING)
                await self._sleep(delay)
            finally:
                self.diag.inflight -= 1

    # -- one volume --------------------------------------------------------------------------
    async def upload_volume(
        self,
        *,
        path: str,
        plan: VolumePlan,
        name: str,
        session: UploadSession | None,
        completed: RangeSet,
        checkpoint: CheckpointSink,
        progress: ProgressTracker,
        base_done: int,
        on_progress: Callable[[ProgressSnapshot], None] | None = None,
    ) -> UploadSession:
        """Upload every missing part of ``plan``. Returns the (possibly replaced) session.

        ``base_done`` = bytes of the logical file completed *before* this volume (for absolute progress).
        """
        restarts = 0
        total_parts = plan.part_count
        self.diag.parts_total = total_parts
        if session is None:
            session = self.gateway.new_upload_session(name)
        dirty = asyncio.Event()
        stop_helpers = asyncio.Event()
        # initial progress = parts already completed (resume)
        progress.rewind(base_done + sum(plan.part_range(i)[1] for i in completed if i < total_parts))

        async def checkpointer() -> None:
            while not stop_helpers.is_set():
                try:
                    await asyncio.wait_for(stop_helpers.wait(), self.config.checkpoint_interval)
                except TimeoutError:
                    pass
                if dirty.is_set():
                    await flush()

        async def flush() -> None:
            dirty.clear()
            snap = (session.file_id, completed.serialize(), progress.done)
            await asyncio.to_thread(checkpoint.save, *snap)
            self.diag.checkpoints_saved += 1
            self.diag.checkpoint_parts = len(completed)

        async def reporter() -> None:
            last = -1
            while not stop_helpers.is_set():
                try:
                    await asyncio.wait_for(stop_helpers.wait(), self.config.progress_interval)
                except TimeoutError:
                    pass
                if on_progress and progress.done != last:
                    last = progress.done
                    try:
                        await asyncio.to_thread(on_progress, progress.snapshot())
                    except Exception:
                        log.exception("progress observer failed")

        helpers = [asyncio.create_task(checkpointer()), asyncio.create_task(reporter())]
        self.set_state(TransferState.UPLOADING)
        try:
            while True:
                try:
                    await self._run_workers(path, plan, name, session, completed, progress, dirty)
                    break
                except UploadSessionInvalid:
                    if restarts >= self.config.max_session_restarts:
                        raise RetriesExhausted("Telegram repeatedly rejected the upload session") from None
                    restarts += 1
                    self.diag.session_restarts += 1
                    log.warning("upload session invalid; starting a new session", extra={"data": {"restart": restarts}})
                    session = self.gateway.new_upload_session(name)
                    completed.clear()
                    progress.rewind(base_done)
                    dirty.set()
                    await flush()
            self.diag.throttled_seconds = self.limiter.total_throttled_seconds
        finally:
            stop_helpers.set()
            await asyncio.gather(*helpers, return_exceptions=True)
            try:
                await flush()  # always persist the latest safe point (pause, failure, cancel, success)
            except Exception:
                log.exception("final checkpoint failed")
            if on_progress:
                try:
                    await asyncio.to_thread(on_progress, progress.snapshot())
                except Exception:
                    log.exception("final progress callback failed")
            log.info("volume upload finished", extra={"data": self.diag.as_dict()})
        return session

    async def _run_workers(
        self,
        path: str,
        plan: VolumePlan,
        name: str,
        session: UploadSession,
        completed: RangeSet,
        progress: ProgressTracker,
        dirty: asyncio.Event,
    ) -> None:
        total = plan.part_count
        todo = iter([i for i in range(total) if i not in completed])

        async def worker() -> None:
            while True:
                if self.control.paused:
                    self.set_state(TransferState.PAUSED)
                await self.control.wait_ready()  # pause stops *new* work only
                if self._state == TransferState.PAUSED:
                    self.set_state(TransferState.UPLOADING)
                idx = next(todo, None)
                if idx is None:
                    return
                offset, length = plan.part_range(idx)
                data = await asyncio.to_thread(_read_part, path, offset, length)
                await self._send_part(session, idx, total, data)
                completed.add(idx)
                progress.add(length)
                self.diag.parts_uploaded += 1
                self.diag.bytes_uploaded += length
                dirty.set()
                if self._state in (TransferState.RECONNECTING, TransferState.RETRYING):
                    self.set_state(TransferState.UPLOADING)

        n = max(1, min(self.config.concurrency, total))
        tasks = [asyncio.create_task(worker()) for _ in range(n)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


def file_signature(path: str) -> tuple[int, int]:
    st = os.stat(_long(path))
    return st.st_size, st.st_mtime_ns
