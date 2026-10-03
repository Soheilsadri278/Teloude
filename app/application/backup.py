"""Backup orchestration: scan -> compare -> hash -> duplicate decision -> upload -> record mapping.

A file is only ever marked BACKED_UP (in one DB transaction together with its Telegram message ids)
after *every* volume's final ``send`` was acknowledged by Telegram.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..domain.errors import (
    PermanentTransferError,
    RateLimited,
    RetriesExhausted,
    TopicNotFound,
    TransferCancelled,
    TransientNetworkError,
    UploadSessionInvalid,
)
from ..domain.failure import FailureKind, classify, explain
from ..domain.models import (
    DuplicateAction,
    FileState,
    ProgressSnapshot,
    RunOutcome,
    ScannedFile,
    TransferState,
    UploadLimits,
)
from ..domain.paths import sanitize_topic_title
from ..domain.planning import RangeSet, plan_volumes
from ..domain.progress import ProgressTracker
from ..domain.ratelimit import TokenBucket
from ..domain.redact import redact
from ..domain.retry import Backoff
from ..infrastructure.repositories import Folder, Repos
from .control import TransferControl
from .metadata import VolumeMeta, encode_caption, volume_filename
from .pipeline import PipelineConfig, UploadPipeline, file_signature
from .ports import StorageRef, TelegramGateway, UploadSession
from .scanner import DEFAULT_IGNORE, hash_file, scan_folder

log = logging.getLogger("teloude.backup")


@dataclass
class DuplicateInfo:
    rel_path: str
    size: int
    existing_paths: list[str]


@dataclass
class DuplicateDecision:
    action: DuplicateAction
    apply_to_all: bool = False


DuplicateResolver = Callable[[DuplicateInfo], Awaitable[DuplicateDecision]]


async def skip_all_duplicates(_info: DuplicateInfo) -> DuplicateDecision:  # explicit, opt-in policy only
    return DuplicateDecision(DuplicateAction.SKIP, True)


def _noop(*_a: object, **_k: object) -> None:
    return None


@dataclass
class BackupEvents:
    on_phase: Callable[[str], None] = _noop
    on_scan: Callable[[int], None] = _noop
    on_file_start: Callable[[int, str, int, int, int], None] = _noop  # file_id, rel, size, index, total
    on_file_state: Callable[[int, TransferState], None] = _noop
    on_file_progress: Callable[[int, ProgressSnapshot], None] = _noop
    on_file_done: Callable[[int, str, str, str | None], None] = _noop  # id, rel, status, error


@dataclass
class RunSummary:
    run_id: int
    kind: str
    storage_id: int
    outcome: RunOutcome = RunOutcome.RUNNING
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    unchanged: int = 0
    cancelled: int = 0
    bytes_done: int = 0
    missing_locally: int = 0
    errors: list[str] = field(default_factory=list)
    # --- failure breakdown (drives the "Backup failed / Resume" UI) ---------------------------
    recoverable: int = 0  # failed, but Resume can continue from the last checkpoint
    permanent: int = 0  # failed and needs a human before it is worth retrying
    unattempted: int = 0  # queued but never started because the run stopped early
    reason: str = ""  # short, secret-free headline for the UI

    @property
    def resumable(self) -> bool:
        """True when a Resume action can do real work (failed/partial runs with recoverable items)."""
        return self.recoverable > 0 or self.unattempted > 0


class _DbCheckpoint:
    def __init__(self, repos: Repos, transfer_id: int):
        self.repos, self.transfer_id = repos, transfer_id

    def save(self, tg_file_id: int, completed_parts: str, done_bytes: int) -> None:
        self.repos.transfers.update(
            self.transfer_id, tg_file_id=tg_file_id, completed_parts=completed_parts, done_bytes=done_bytes
        )


@dataclass
class _Ctx:
    storage_id: int
    ref: StorageRef
    limits: UploadLimits
    control: TransferControl
    events: BackupEvents
    resolver: DuplicateResolver
    summary: RunSummary
    pcfg: PipelineConfig
    apply_all: DuplicateDecision | None = None


class BackupService:
    def __init__(
        self,
        repos: Repos,
        gateway: TelegramGateway,
        limiter: TokenBucket,
        pipeline_config: Callable[[], PipelineConfig] = PipelineConfig,
        ignore: Callable[[], tuple[str, ...]] = lambda: DEFAULT_IGNORE,
    ):
        self.repos = repos
        self.gateway = gateway
        self.limiter = limiter
        self._pipeline_config = pipeline_config
        self._ignore = ignore
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="teloude-db-writer")

    # -- public API ---------------------------------------------------------------------------
    async def run(
        self,
        storage_id: int,
        local_root: str,
        resolver: DuplicateResolver,
        control: TransferControl | None = None,
        events: BackupEvents | None = None,
    ) -> RunSummary:
        control = control or TransferControl()
        events = events or BackupEvents()
        srow = self.repos.storages.get(storage_id)
        if srow is None:
            raise PermanentTransferError("unknown storage")
        ref = StorageRef(srow["tg_chat_id"], srow["tg_access_hash"], srow["title"], srow["uuid"])
        run_id = self.repos.runs.start("backup", storage_id)
        summary = RunSummary(run_id, "backup", storage_id)
        self.repos.storages.set_local_root(storage_id, local_root)
        try:
            limits = await self.gateway.get_limits()
            ctx = _Ctx(storage_id, ref, limits, control, events, resolver, summary, self._pipeline_config())
            await self._run(ctx, local_root)
        except TransferCancelled:
            summary.outcome = RunOutcome.CANCELLED
        except Exception as exc:  # setup-level failure (e.g. scan root missing)
            log.exception("backup run failed")
            summary.errors.append(redact(exc))
            summary.outcome = RunOutcome.FAILED
        finally:
            self._finish(summary)
        events.on_phase("done")
        return summary

    # -- internals ----------------------------------------------------------------------------
    async def _run(self, ctx: _Ctx, local_root: str) -> None:
        s, ev, control = ctx.summary, ctx.events, ctx.control
        ev.on_phase("scanning")
        index = await asyncio.to_thread(self.repos.files.current_index, ctx.storage_id)
        scan = await asyncio.to_thread(scan_folder, local_root, index, self._ignore(), control.stop_requested, ev.on_scan)
        s.unchanged = len(scan.unchanged)
        s.missing_locally = len(scan.missing_locally)  # informational: NOTHING is deleted anywhere
        for path, err in scan.errors[:50]:
            s.errors.append(redact(f"cannot read {path}: {err}"))
        ev.on_phase("comparing")
        todo = await asyncio.to_thread(self._register, ctx.storage_id, scan.pending)
        s.total = len(todo)
        if not todo:
            s.outcome = RunOutcome.NOTHING_TO_DO if not scan.errors else RunOutcome.PARTIAL
            return
        ev.on_phase("uploading")
        aborted = False
        for i, (file_id, sf) in enumerate(todo):
            try:
                await control.wait_ready()
                ev.on_file_start(file_id, sf.rel_path, sf.size, i, len(todo))
                status = await self._process_file(ctx, file_id, sf)
                if status == "backed_up":
                    s.succeeded += 1
                    s.bytes_done += sf.size
                elif status == "skipped":
                    s.skipped += 1
                else:  # 'unchanged'
                    s.unchanged += 1
                ev.on_file_done(file_id, sf.rel_path, status, None)
            except TransferCancelled:
                await self._drain()
                self._reset_file(file_id, TransferState.CANCELLED)
                s.cancelled += len(todo) - i
                ev.on_file_done(file_id, sf.rel_path, "cancelled", None)
                s.outcome = RunOutcome.CANCELLED
                return
            except RetriesExhausted as exc:
                msg = redact(exc)
                await self._drain()
                self._count_failure(s, self._fail_file(file_id, msg, exc))
                s.failed += 1
                # network is down: stop instead of burning minutes per file. The files we never
                # started are NOT failures - they are simply waiting for the next run.
                s.unattempted += len(todo) - i - 1
                s.errors.append(f"{sf.rel_path}: {msg}")
                if not s.reason:
                    s.reason = explain(exc)
                ev.on_file_done(file_id, sf.rel_path, "failed", msg)
                aborted = True
                break
            except Exception as exc:
                msg = redact(exc) or type(exc).__name__
                log.warning("file failed", extra={"data": {"error": msg, "type": type(exc).__name__}})
                await self._drain()
                self._count_failure(s, self._fail_file(file_id, msg, exc))
                s.failed += 1
                s.errors.append(f"{sf.rel_path}: {msg}")
                if not s.reason:
                    s.reason = explain(exc)
                ev.on_file_done(file_id, sf.rel_path, "failed", msg)
        if aborted:
            s.errors.append("Run stopped early: Telegram is unreachable. Resume later - progress is saved.")
        if not s.reason and s.recoverable:
            s.reason = "Connection lost"
        if not s.reason and s.permanent:
            s.reason = "Needs your attention"
        if s.outcome == RunOutcome.RUNNING:
            if s.failed and not s.succeeded:
                s.outcome = RunOutcome.FAILED
            elif s.failed or s.errors and scan.errors:
                s.outcome = RunOutcome.PARTIAL
            else:
                s.outcome = RunOutcome.COMPLETED

    def _finish(self, s: RunSummary) -> None:
        if s.outcome == RunOutcome.RUNNING:
            s.outcome = RunOutcome.FAILED
        self.repos.runs.finish(
            s.run_id, s.outcome, s.total, s.succeeded, s.failed, s.skipped, s.cancelled, s.bytes_done,
            "; ".join(s.errors[:5]),
        )  # fmt: skip

    def _register(self, storage_id: int, pending: list[ScannedFile]) -> list[tuple[int, ScannedFile]]:
        out: list[tuple[int, ScannedFile]] = []
        folder_ids: dict[str, int] = {}
        with self.repos.db_transaction():
            for sf in pending:
                dirname = posixpath.dirname(sf.rel_path)
                if dirname not in folder_ids:
                    folder_ids[dirname] = self.repos.folders.ensure(storage_id, dirname).id
                out.append((self.repos.files.upsert_pending(storage_id, folder_ids[dirname], sf), sf))
        return out

    def _bg(self, fn: Callable[..., object], *args: object) -> None:
        """Fire-and-forget DB write on one ordered thread so the event loop never blocks on SQLite."""
        self._writer.submit(self._safe, fn, *args)

    @staticmethod
    def _safe(fn: Callable[..., object], *args: object) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("background db write failed")

    async def _drain(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._writer, _noop)

    def _reset_file(self, file_id: int, tstate: TransferState) -> None:
        self.repos.files.set_state(file_id, FileState.PENDING)
        t = self.repos.transfers.resumable_or_active_for_file(file_id)
        if t:
            self.repos.transfers.update(t["id"], state=tstate)

    @staticmethod
    def _count_failure(s: RunSummary, kind: FailureKind) -> None:
        if kind == FailureKind.RECOVERABLE:
            s.recoverable += 1
        else:
            s.permanent += 1

    def _fail_file(self, file_id: int, msg: str, exc: BaseException | None = None) -> FailureKind:
        """Record a failed file AND keep enough state to resume it later.

        The failure is classified so the UI can offer Resume for a lost connection and must not
        offer it for something only a human can fix.
        """
        kind = classify(exc) if exc is not None else FailureKind.RECOVERABLE
        file_state = FileState.FAILED if kind == FailureKind.RECOVERABLE else FileState.PERMANENT_FAILED
        transfer_state = (
            TransferState.RECOVERABLE_FAILED if kind == FailureKind.RECOVERABLE else TransferState.PERMANENT_FAILED
        )
        self.repos.files.set_state(file_id, file_state, msg)
        t = self.repos.transfers.resumable_or_active_for_file(file_id)
        if t:
            self.repos.transfers.update(t["id"], state=transfer_state, error=msg)
        return kind

    # -- one file -------------------------------------------------------------------------------
    async def _process_file(self, ctx: _Ctx, file_id: int, sf: ScannedFile) -> str:
        repos, control = self.repos, ctx.control
        row = repos.files.get(file_id)
        assert row is not None
        ctx.events.on_file_state(file_id, TransferState.HASHING)
        sha = row["sha256"]
        if not sha:
            sha, size_read, mtime_after = await asyncio.to_thread(hash_file, sf.abs_path, control.stop_requested)
            sf.size, sf.mtime_ns = size_read, mtime_after  # consistent with the bytes we hashed
            repos.files.set_hash(file_id, sha, size_read, mtime_after)
        if repos.files.revert_if_identical(file_id, sha, sf.size, sf.mtime_ns):
            return "unchanged"
        dupes = repos.files.find_duplicates(ctx.storage_id, sha, sf.size, exclude_id=file_id)
        if dupes:
            decision = ctx.apply_all or await ctx.resolver(DuplicateInfo(sf.rel_path, sf.size, [d["rel_path"] for d in dupes]))
            if decision.apply_to_all:
                ctx.apply_all = decision
            if decision.action == DuplicateAction.CANCEL:
                control.cancel()
                raise TransferCancelled("cancelled at duplicate prompt")
            if decision.action == DuplicateAction.SKIP:
                repos.files.set_state(file_id, FileState.DUPLICATE_SKIPPED)
                return "skipped"
        await self._upload_file(ctx, file_id, sf, sha)
        return "backed_up"

    async def _ensure_topic(self, ctx: _Ctx, folder: Folder, force_new: bool = False) -> int:
        if folder.tg_topic_id and not force_new:
            return folder.tg_topic_id
        title = sanitize_topic_title(" / ".join(folder.logical_path.split("/")))
        topic_id = await self.gateway.create_topic(ctx.ref, title)
        self.repos.folders.set_topic(folder.id, topic_id)
        return topic_id

    async def _upload_file(self, ctx: _Ctx, file_id: int, sf: ScannedFile, sha: str) -> None:
        repos, control = self.repos, ctx.control
        row = repos.files.get(file_id)
        assert row is not None
        folder = repos.folders.get(row["folder_id"])
        assert folder is not None
        volumes = plan_volumes(sf.size, ctx.limits)
        meta_name = row["name"]

        tr = repos.transfers.resumable_for_file(file_id)
        if tr is not None and not (
            tr["src_size"] == sf.size
            and tr["src_mtime_ns"] == sf.mtime_ns
            and tr["volume_count"] == len(volumes)
            and tr["part_size"] == ctx.limits.part_size
        ):
            repos.transfers.delete_for_file(file_id)  # source changed: the old checkpoint is meaningless
            tr = None
        if tr is None:
            tid = repos.transfers.create("backup", file_id, ctx.storage_id, sf.size, len(volumes), ctx.summary.run_id, sf.size, sf.mtime_ns)
            repos.transfers.update(tid, part_size=ctx.limits.part_size)
            tr = repos.transfers.get(tid)
        assert tr is not None
        tid = tr["id"]
        repos.transfers.update(tid, state=TransferState.UPLOADING, error=None)
        repos.files.set_state(file_id, FileState.UPLOADING)

        progress = ProgressTracker(sf.size)
        pipeline = UploadPipeline(
            self.gateway, self.limiter, control, ctx.pcfg,
            on_state=lambda st: self._on_state(ctx, file_id, tid, st),
        )  # fmt: skip
        topic_id = await self._ensure_topic(ctx, folder)
        volume_msgs: list[int] = json.loads(tr["volume_messages"] or "[]")
        base_done = sum(v.length for v in volumes[: len(volume_msgs)])

        for vol in volumes[len(volume_msgs) :]:
            vi = vol.index
            restarts = 0
            same_vol = tr["volume_index"] == vi
            session: UploadSession | None = None
            completed = RangeSet()
            if same_vol and tr["tg_file_id"]:
                session = UploadSession(int(tr["tg_file_id"]), volume_filename(meta_name, vi, len(volumes)))
                completed = RangeSet.parse(tr["completed_parts"])
                if completed and not await self.gateway.validate_upload_session(session):
                    # The server no longer knows this partial upload. Drop the checkpoint we cannot
                    # honour instead of reporting bytes we would have to send again.
                    log.info(
                        "persisted upload session is gone; starting a new one",
                        extra={"data": {"file_id": file_id, "volume": vi, "parts_lost": len(completed)}},
                    )
                    pipeline.diag.session_restarts += 1
                    repos.transfers.bump(tid, "session_restarts")
                    session, completed = None, RangeSet()
                    repos.transfers.update(tid, tg_file_id=None, completed_parts="")
                elif not completed:
                    # Nothing was acknowledged yet: a fresh session costs nothing and cannot be stale.
                    session = None
            adopted: int | None = None
            if same_vol and tr["finalizing"]:
                # We may have crashed *after* Telegram accepted the message but before recording it.
                adopted = await self.gateway.find_volume(ctx.ref, topic_id, sha, vi)
            repos.transfers.update(tid, volume_index=vi, finalizing=0)
            fname = volume_filename(meta_name, vi, len(volumes))
            caption = encode_caption(
                VolumeMeta(sf.rel_path, sha, sf.size, sf.mtime_ns, vi, len(volumes), row["version"], f"{sha[:16]}.{row['version']}"),
                ctx.limits.caption_limit,
            )
            message_id = adopted
            while message_id is None:
                session = await pipeline.upload_volume(
                    path=sf.abs_path, plan=vol, name=fname, session=session, completed=completed,
                    checkpoint=_DbCheckpoint(repos, tid), progress=progress, base_done=base_done,
                    on_progress=lambda snap: ctx.events.on_file_progress(file_id, snap),
                )  # fmt: skip
                if await asyncio.to_thread(file_signature, sf.abs_path) != (sf.size, sf.mtime_ns):
                    raise PermanentTransferError("file changed while it was being uploaded; it will be retried next run")
                repos.transfers.update(tid, finalizing=1)
                try:
                    message_id, topic_id = await self._finalize(ctx, folder, topic_id, session, vol.part_count, fname, caption, control)
                except UploadSessionInvalid:
                    restarts += 1
                    if restarts > ctx.pcfg.max_session_restarts:
                        raise RetriesExhausted("Telegram rejected the upload session repeatedly") from None
                    pipeline.diag.session_restarts += 1
                    repos.transfers.bump(tid, "session_restarts")
                    repos.transfers.update(tid, tg_file_id=None, completed_parts="", finalizing=0)
                    session, completed = None, RangeSet()
                    progress.rewind(base_done)
            volume_msgs.append(message_id)
            base_done += vol.length
            repos.transfers.update(
                tid, volume_messages=json.dumps(volume_msgs), tg_file_id=None, completed_parts="", finalizing=0, done_bytes=base_done
            )
            same_vol = False
            tr = repos.transfers.get(tid) or tr

        await self._drain()
        records = [(i, len(volumes), mid, topic_id, volumes[i].length) for i, mid in enumerate(volume_msgs)]
        repos.files.mark_backed_up(file_id, records)  # <- the ONLY place a file becomes BACKED_UP
        repos.transfers.update(
            tid, state=TransferState.COMPLETED, done_bytes=sf.size,
            retry_count=pipeline.diag.retries, reconnect_count=pipeline.diag.reconnects,
        )  # fmt: skip
        ctx.events.on_file_state(file_id, TransferState.COMPLETED)

    def _on_state(self, ctx: _Ctx, file_id: int, tid: int, st: TransferState) -> None:
        ctx.events.on_file_state(file_id, st)
        self._bg(lambda: self.repos.transfers.update(tid, state=st))

    async def _finalize(
        self, ctx: _Ctx, folder: Folder, topic_id: int, session: UploadSession, parts: int, fname: str, caption: str,
        control: TransferControl,
    ) -> tuple[int, int]:  # fmt: skip
        backoff = Backoff(ctx.pcfg.retry)
        recreated = False
        while True:
            control.raise_if_cancelled()
            try:
                mid = await self.gateway.finalize_upload(ctx.ref, topic_id, session, parts, fname, caption)
                return mid, topic_id
            except TopicNotFound:
                if recreated:
                    raise PermanentTransferError("the Telegram topic for this folder is missing and could not be recreated") from None
                recreated = True
                topic_id = await self._ensure_topic(ctx, folder, force_new=True)
            except RateLimited as exc:
                await ctx.pcfg.sleep(min(exc.seconds + 0.5, ctx.pcfg.flood_wait_cap))
            except (TransientNetworkError, ConnectionError, TimeoutError, OSError) as exc:
                delay = backoff.next_delay()
                if backoff.attempts > backoff.policy.max_attempts:
                    raise RetriesExhausted(f"finalizing failed: {type(exc).__name__}") from exc
                try:
                    await self.gateway.reconnect()
                except (TimeoutError, TransientNetworkError, OSError):
                    pass
                await ctx.pcfg.sleep(delay)
