"""Restore from Telegram to a user-chosen local folder.

Safety: every remote-supplied path goes through ``safe_join`` (no traversal / reserved names /
absolute paths); an existing destination ALWAYS needs an explicit decision; data is written to a
``.teloude-part`` sibling and atomically renamed only after size and SHA-256 verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.errors import (
    PermanentTransferError,
    RateLimited,
    RetriesExhausted,
    TransferCancelled,
    TransientNetworkError,
    UnsafePathError,
)
from ..domain.models import ConflictAction, ProgressSnapshot, RunOutcome
from ..domain.paths import safe_join, unique_sibling
from ..domain.progress import ProgressTracker
from ..domain.ratelimit import TokenBucket
from ..domain.redact import redact
from ..domain.retry import Backoff, RetryPolicy
from ..infrastructure.repositories import Repos
from .backup import RunSummary
from .control import TransferControl
from .ports import StorageRef, TelegramGateway

log = logging.getLogger("teloude.restore")

CHUNK = 512 * 1024  # Telegram requires offsets/limits aligned to this granularity
TEMP_SUFFIX = ".teloude-part"


@dataclass
class ConflictInfo:
    rel_path: str
    dest_path: str
    existing_size: int
    incoming_size: int


@dataclass
class ConflictDecision:
    action: ConflictAction
    apply_to_all: bool = False


ConflictResolver = Callable[[ConflictInfo], Awaitable[ConflictDecision]]


def _noop(*_a: object, **_k: object) -> None:
    return None


@dataclass
class RestoreEvents:
    on_phase: Callable[[str], None] = _noop
    on_file_start: Callable[[int, str, int, int, int], None] = _noop
    on_file_progress: Callable[[int, ProgressSnapshot], None] = _noop
    on_file_done: Callable[[int, str, str, str | None], None] = _noop


@dataclass
class _RCtx:
    dest_root: str
    resolver: ConflictResolver
    control: TransferControl
    events: RestoreEvents
    summary: RunSummary
    apply_all: ConflictDecision | None = None
    refs: dict[int, StorageRef] = field(default_factory=dict)


class RestoreService:
    def __init__(self, repos: Repos, gateway: TelegramGateway, limiter: TokenBucket, retry: RetryPolicy | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):  # fmt: skip
        self.repos, self.gateway, self.limiter = repos, gateway, limiter
        self.retry = retry or RetryPolicy()
        self._sleep = sleep

    def file_ids_for(self, storage_id: int, path_prefix: str | None = None) -> list[int]:
        return [int(r["id"]) for r in self.repos.files.list_backed_up(storage_id, path_prefix)]

    async def restore(
        self,
        file_ids: list[int],
        dest_root: str,
        resolver: ConflictResolver,
        control: TransferControl | None = None,
        events: RestoreEvents | None = None,
    ) -> RunSummary:
        control = control or TransferControl()
        events = events or RestoreEvents()
        first = self.repos.files.get(file_ids[0]) if file_ids else None
        storage_id = int(first["storage_id"]) if first else 0
        run_id = self.repos.runs.start("restore", storage_id or None)
        summary = RunSummary(run_id, "restore", storage_id, total=len(file_ids))
        ctx = _RCtx(dest_root, resolver, control, events, summary)
        events.on_phase("restoring")
        try:
            await self._run(ctx, file_ids)
        except TransferCancelled:
            summary.outcome = RunOutcome.CANCELLED
        except Exception as exc:
            log.exception("restore failed")
            summary.errors.append(redact(exc))
            summary.outcome = RunOutcome.FAILED
        finally:
            if summary.outcome == RunOutcome.RUNNING:
                summary.outcome = RunOutcome.FAILED
            self.repos.runs.finish(
                run_id, summary.outcome, summary.total, summary.succeeded, summary.failed, summary.skipped,
                summary.cancelled, summary.bytes_done, "; ".join(summary.errors[:5]),
            )  # fmt: skip
        events.on_phase("done")
        return summary

    async def _run(self, ctx: _RCtx, file_ids: list[int]) -> None:
        s = ctx.summary
        for i, fid in enumerate(file_ids):
            row = self.repos.files.get(fid)
            if row is None:
                s.failed += 1
                continue
            rel = row["rel_path"]
            try:
                await ctx.control.wait_ready()
                ctx.events.on_file_start(fid, rel, row["size"], i, len(file_ids))
                status = await self._restore_file(ctx, row)
                if status == "restored":
                    s.succeeded += 1
                    s.bytes_done += row["size"]
                else:
                    s.skipped += 1
                ctx.events.on_file_done(fid, rel, status, None)
            except TransferCancelled:
                s.cancelled += len(file_ids) - i
                ctx.events.on_file_done(fid, rel, "cancelled", None)
                s.outcome = RunOutcome.CANCELLED
                return
            except RetriesExhausted as exc:
                msg = redact(exc)
                s.failed += len(file_ids) - i
                s.errors.append(f"{rel}: {msg}")
                ctx.events.on_file_done(fid, rel, "failed", msg)
                break
            except (UnsafePathError, PermanentTransferError, OSError) as exc:
                msg = redact(exc)
                s.failed += 1
                s.errors.append(f"{rel}: {msg}")
                ctx.events.on_file_done(fid, rel, "failed", msg)
            except Exception as exc:
                msg = redact(exc) or type(exc).__name__
                log.exception("restore of one file failed")
                s.failed += 1
                s.errors.append(f"{rel}: {msg}")
                ctx.events.on_file_done(fid, rel, "failed", msg)
        if s.outcome == RunOutcome.RUNNING:
            if s.failed and not s.succeeded:
                s.outcome = RunOutcome.FAILED
            elif s.failed:
                s.outcome = RunOutcome.PARTIAL
            elif s.total and not s.succeeded and s.skipped:
                s.outcome = RunOutcome.NOTHING_TO_DO
            else:
                s.outcome = RunOutcome.COMPLETED

    def _ref(self, ctx: _RCtx, storage_id: int) -> StorageRef:
        if storage_id not in ctx.refs:
            r = self.repos.storages.get(storage_id)
            if r is None:
                raise PermanentTransferError("storage is no longer known on this PC")
            ctx.refs[storage_id] = StorageRef(r["tg_chat_id"], r["tg_access_hash"], r["title"], r["uuid"])
        return ctx.refs[storage_id]

    async def _restore_file(self, ctx: _RCtx, row) -> str:  # type: ignore[no-untyped-def]
        msgs = self.repos.messages.for_file(row["id"])
        if not msgs or len(msgs) != row["volumes_total"]:
            raise PermanentTransferError("cloud record is incomplete (missing Telegram message mapping)")
        dest = safe_join(ctx.dest_root, row["rel_path"])  # may raise UnsafePathError
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        replace = False
        if dest.exists():
            decision = ctx.apply_all or await ctx.resolver(
                ConflictInfo(row["rel_path"], str(dest), dest.stat().st_size, row["size"])
            )
            if decision.apply_to_all:
                ctx.apply_all = decision
            if decision.action == ConflictAction.CANCEL:
                ctx.control.cancel()
                raise TransferCancelled("cancelled at conflict prompt")
            if decision.action == ConflictAction.SKIP:
                return "skipped"
            if decision.action == ConflictAction.KEEP_BOTH:
                dest = unique_sibling(dest)
            else:
                replace = True
        temp = dest.with_name(dest.name + TEMP_SUFFIX)
        ref = self._ref(ctx, row["storage_id"])
        await self._download_to(ctx, row, msgs, ref, temp)
        # verified: move into place
        if replace:
            await asyncio.to_thread(os.replace, temp, dest)
        else:
            if dest.exists():  # appeared while we were downloading: never clobber it
                dest = unique_sibling(dest)
            await asyncio.to_thread(os.replace, temp, dest)
        try:
            os.utime(dest, ns=(row["mtime_ns"], row["mtime_ns"]))
        except OSError:
            pass
        return "restored"

    async def _download_to(self, ctx: _RCtx, row, msgs, ref: StorageRef, temp: Path) -> None:  # type: ignore[no-untyped-def]
        total = sum(m["size"] for m in msgs)
        hasher = hashlib.sha256()
        existing = temp.stat().st_size if temp.exists() else 0
        existing = min(existing, total)
        existing = (existing // CHUNK) * CHUNK  # resume only on an aligned boundary
        mode = "r+b" if temp.exists() else "wb"
        progress = ProgressTracker(total, initial=existing)
        if existing:
            await asyncio.to_thread(self._rehash_prefix, temp, existing, hasher)
        last_emit = 0.0
        backoff = Backoff(self.retry)
        with open(temp, mode) as fh:
            fh.truncate(existing)
            fh.seek(existing)
            written = existing
            vstart = 0
            for m in msgs:
                vsize = m["size"]
                vend = vstart + vsize
                while written < vend:
                    try:
                        inner = written - vstart
                        async for chunk in self.gateway.iter_download(ref, m["message_id"], inner, CHUNK):
                            ctx.control.raise_if_cancelled()
                            await ctx.control.wait_ready()
                            chunk = chunk[: vend - written]
                            await self.limiter.acquire(len(chunk))
                            await asyncio.to_thread(fh.write, chunk)
                            hasher.update(chunk)
                            written += len(chunk)
                            progress.add(len(chunk))
                            backoff = Backoff(self.retry)  # success resets the (scoped) backoff
                            last_emit = self._emit(ctx, row["id"], progress, last_emit)
                            if written >= vend:
                                break
                        if written < vend:
                            raise PermanentTransferError("Telegram returned less data than recorded")
                    except RateLimited as exc:
                        await self._sleep(min(exc.seconds + 0.5, 900))
                    except (TransientNetworkError, ConnectionError, TimeoutError, OSError) as exc:
                        if isinstance(exc, OSError) and not isinstance(exc, (ConnectionError, TimeoutError)):
                            raise
                        delay = backoff.next_delay()
                        if backoff.attempts > backoff.policy.max_attempts:
                            raise RetriesExhausted(f"download failed: {type(exc).__name__}") from exc
                        try:
                            await self.gateway.reconnect()
                        except (TimeoutError, TransientNetworkError, OSError):
                            pass
                        await self._sleep(delay)
                        # resume from the last whole chunk boundary inside this volume
                        aligned = vstart + ((written - vstart) // CHUNK) * CHUNK
                        if aligned != written:
                            await asyncio.to_thread(fh.truncate, aligned)
                            fh.seek(aligned)
                            hasher = hashlib.sha256()
                            await asyncio.to_thread(self._rehash_prefix, temp, aligned, hasher, fh)
                            progress.rewind(aligned)
                            written = aligned
                vstart = vend
            fh.flush()
            os.fsync(fh.fileno())
        self._emit(ctx, row["id"], progress, 0.0)
        if written != total or total != row["size"]:
            raise PermanentTransferError("size mismatch after download; partial file kept for resume")
        expected = row["sha256"]
        if expected and hasher.hexdigest() != expected:
            try:
                temp.unlink()  # our own corrupt temp file - never a user file
            except OSError:
                pass
            raise PermanentTransferError("checksum mismatch: downloaded data does not match the backup record")

    @staticmethod
    def _rehash_prefix(path: Path, nbytes: int, hasher, fh=None) -> None:  # type: ignore[no-untyped-def]
        if fh is not None:
            fh.flush()
        with open(path, "rb") as rf:
            left = nbytes
            while left > 0:
                b = rf.read(min(4 * 1024 * 1024, left))
                if not b:
                    break
                hasher.update(b)
                left -= len(b)

    def _emit(self, ctx: _RCtx, file_id: int, progress: ProgressTracker, last: float) -> float:
        import time

        now = time.monotonic()
        if now - last >= 0.25 or last == 0.0:
            ctx.events.on_file_progress(file_id, progress.snapshot())
            return now
        return last
