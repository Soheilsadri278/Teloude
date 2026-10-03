"""AppController: the only place where Qt signals meet the asynchronous application services."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal

from ..application.backup import BackupEvents, BackupService, DuplicateDecision, DuplicateInfo, RunSummary
from ..application.control import TransferControl
from ..application.notifications import NotificationService
from ..application.pipeline import PipelineConfig
from ..application.ports import TelegramGateway
from ..application.preview import PreviewService
from ..application.recovery import RecoveryService
from ..application.restore import ConflictDecision, ConflictInfo, RestoreEvents, RestoreService
from ..application.resumable import ResumeService, ResumeSummary
from ..application.search import SearchService
from ..application.settings import AppSettings, SettingsService
from ..application.storage_service import StorageService
from ..domain.errors import (
    InvalidCode,
    InvalidPassword,
    MissingApiCredentials,
    PasswordRequired,
    ProxyUnsupported,
    TransientNetworkError,
)
from ..domain.models import (
    DuplicateAction,
    ProgressSnapshot,
    ProxySettings,
    ProxyState,
    TransferState,
)
from ..domain.ratelimit import TokenBucket
from ..domain.redact import redact
from ..domain.retry import RetryPolicy
from ..infrastructure.build_config import bundled_api_credentials
from ..infrastructure.config import AppPaths
from ..infrastructure.db import open_database
from ..infrastructure.repositories import Repos
from ..infrastructure.secrets import create_secret_store

log = logging.getLogger("teloude.controller")


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, MissingApiCredentials):
        return "Telegram API credentials are missing. Enter them to continue."
    if isinstance(exc, InvalidCode):
        return str(exc) or "That code is not valid."
    if isinstance(exc, InvalidPassword):
        return "That password is not correct."
    if isinstance(exc, ProxyUnsupported):
        return str(exc)
    if isinstance(exc, (TransientNetworkError, ConnectionError, TimeoutError, OSError)):
        return "Can't reach Telegram. Check your connection or proxy and try again."
    return redact(exc) or type(exc).__name__


@dataclass
class ActiveOp:
    kind: str
    label: str
    control: TransferControl
    storage_id: int | None = None


class AppController(QObject):
    login_state = Signal(str, str)  # state, detail
    proxy_state = Signal(str, str)
    proxy_test_result = Signal(bool, str)
    storages_changed = Signal()
    op_started = Signal(str, str)  # kind, label
    op_phase = Signal(str)
    file_event = Signal(int, str, str, float, float, float, float)  # id, rel, state, done, total, speed, eta(-1)
    op_progress = Signal(float, float, float, float)  # done, total, speed, eta(-1) - whole operation
    op_finished = Signal(object)  # RunSummary
    resumable_changed = Signal(object)  # ResumeSummary - what "Resume" would continue
    paused_changed = Signal(bool)
    notify = Signal(str, str, str)
    ask_duplicate = Signal(object, object)
    ask_conflict = Signal(object, object)
    message = Signal(str)  # transient info/error toast text
    account_changed = Signal(str)
    _dispatch = Signal(object)

    def __init__(self, paths: AppPaths, gateway: TelegramGateway | None = None, runner=None):  # type: ignore[no-untyped-def]
        super().__init__()
        from .runner import AsyncRunner

        self.paths = paths
        self.db = open_database(paths.db)
        self.repos = Repos.create(self.db)
        self.secrets = create_secret_store(paths.secrets)
        self.settings = SettingsService(self.repos.settings, self.secrets)
        s = self.settings.load()
        self.limiter = TokenBucket(s.speed_bytes_per_second)
        if gateway is None:
            from ..infrastructure.telethon_gateway import TelethonGateway

            gateway = TelethonGateway(self.settings.get_api_credentials, self.secrets, self.settings.get_proxy)
        self.gateway = gateway
        self.gateway.set_state_listener(lambda st, detail: self.proxy_state.emit(st.value, detail or ""))
        self.backup = BackupService(self.repos, gateway, self.limiter, self._pipeline_config, lambda: self.settings.load().ignore_patterns)
        self.restore = RestoreService(self.repos, gateway, self.limiter, RetryPolicy())
        self.storages = StorageService(self.repos, gateway)
        self.search = SearchService(self.repos)
        self.preview = PreviewService(self.repos, gateway, paths.cache, s.preview_max_mb)
        self.notifications = NotificationService(
            self.repos.runs, lambda n: self.notify.emit(n.title, n.message, n.kind), lambda: self.settings.load().notifications
        )
        self.runner = runner or AsyncRunner()
        self.active: ActiveOp | None = None
        self.last_op: ActiveOp | None = None  # keeps (kind, storage, label) so a failed run can be resumed
        self.last_summary: RunSummary | None = None
        self.resume_service = ResumeService(self.repos)
        self._progress: dict[int, tuple[float, float]] = {}  # file_id -> (done, total) acknowledged
        self._resumable = ResumeSummary()
        self.account_name = ""
        # True when this build ships its own Telegram API credentials, so the login flow can skip
        # straight to phone -> code -> 2FA. Never exposes the values themselves to the UI.
        self.bundled_credentials = bundled_api_credentials() is not None
        self._dispatch.connect(lambda fn: fn())
        self.recovery = RecoveryService(self.repos).recover()

    # ------------------------------------------------------------------ plumbing
    def _pipeline_config(self) -> PipelineConfig:
        return PipelineConfig(concurrency=self.settings.load().concurrency)

    def run_async(self, coro: Coroutine[Any, Any, Any], ok: Callable[[Any], None] | None = None,
                  err: Callable[[BaseException], None] | None = None) -> None:  # fmt: skip
        fut = self.runner.submit(coro)

        def done(f: concurrent.futures.Future[Any]) -> None:
            error = f.exception()
            if error is not None:
                if err:
                    self._dispatch.emit(lambda e=error: err(e))  # type: ignore[misc]
                else:
                    self._dispatch.emit(lambda e=error: self.message.emit(friendly_error(e)))  # type: ignore[misc]
                return
            if ok:
                result = f.result()
                self._dispatch.emit(lambda r=result: ok(r))  # type: ignore[misc]

        fut.add_done_callback(done)

    def start(self) -> None:
        self.runner.start()
        self.run_async(self._bootstrap(), err=lambda e: self.login_state.emit("offline", friendly_error(e)))

    def shutdown(self) -> None:
        if self.active:
            self.active.control.cancel()
        if self.runner.loop.is_running():
            try:
                self.runner.submit(self.gateway.disconnect()).result(5)
            except Exception:
                log.warning("gateway disconnect on exit failed")
        self.runner.stop()
        self.db.close()

    @property
    def busy(self) -> bool:
        return self.active is not None

    # ------------------------------------------------------------------ login
    async def _bootstrap(self) -> None:
        if self.settings.get_api_credentials() is None:
            self._dispatch.emit(lambda: self.login_state.emit("need_credentials", ""))
            return
        await self.gateway.connect()
        await self._after_connect()

    async def _after_connect(self) -> None:
        if await self.gateway.is_authorized():
            name = (await self.gateway.account()).display_name
            self.account_name = name
            self._dispatch.emit(lambda: (self.account_changed.emit(name), self.login_state.emit("ready", name)))
            await self.storages.discover()
            self._dispatch.emit(self.storages_changed.emit)
        else:
            self._dispatch.emit(lambda: self.login_state.emit("need_phone", ""))

    def retry_connect(self) -> None:
        self.start_bootstrap()

    def start_bootstrap(self) -> None:
        self.run_async(self._bootstrap(), err=lambda e: self.login_state.emit("offline", friendly_error(e)))

    def submit_credentials(self, api_id: str, api_hash: str) -> None:
        try:
            self.settings.set_api_credentials(int(api_id.strip()), api_hash.strip())
        except ValueError:
            self.login_state.emit("need_credentials", "API ID must be a number.")
            return
        self.run_async(self._apply_creds(), err=lambda e: self.login_state.emit("offline", friendly_error(e)))

    async def _apply_creds(self) -> None:
        await self.gateway.apply_proxy(self.settings.get_proxy())  # rebuilds the client with the new credentials
        await self._after_connect()

    def submit_phone(self, phone: str) -> None:
        self._phone = phone.strip()

        async def go() -> None:
            await self.gateway.send_code(self._phone)

        self.run_async(go(), ok=lambda _: self.login_state.emit("need_code", ""),
                       err=lambda e: self.login_state.emit("need_phone", friendly_error(e)))  # fmt: skip

    def submit_code(self, code: str) -> None:
        async def go() -> None:
            try:
                await self.gateway.sign_in_code(self._phone, code.strip())
            except PasswordRequired:
                self._dispatch.emit(lambda: self.login_state.emit("need_password", ""))
                return
            await self._after_connect()

        self.run_async(go(), err=lambda e: self.login_state.emit("need_code", friendly_error(e)))

    def submit_password(self, password: str) -> None:
        async def go() -> None:
            await self.gateway.sign_in_password(password)
            await self._after_connect()

        self.run_async(go(), err=lambda e: self.login_state.emit("need_password", friendly_error(e)))

    def sign_out(self) -> None:
        async def go() -> None:
            await self.gateway.sign_out()

        self.run_async(go(), ok=lambda _: self.login_state.emit("need_phone", ""))

    # ------------------------------------------------------------------ proxy
    def get_proxy(self) -> ProxySettings:
        return self.settings.get_proxy()

    def save_proxy(self, proxy: ProxySettings) -> None:
        """Persist + apply globally (auth, uploads, downloads, search: they all share one client)."""
        self.settings.set_proxy(proxy)
        self.proxy_state.emit(ProxyState.CONNECTING.value if proxy.enabled else ProxyState.DISCONNECTED.value, "")
        self.run_async(
            self.gateway.apply_proxy(proxy),
            err=lambda e: (self.proxy_state.emit(ProxyState.ERROR.value, friendly_error(e)), self.message.emit(friendly_error(e))),
        )

    def test_proxy(self, proxy: ProxySettings) -> None:
        self.proxy_state.emit(ProxyState.CONNECTING.value, "")

        def ok(_: Any) -> None:
            self.proxy_test_result.emit(True, "Connected through the proxy.")
            self.proxy_state.emit(ProxyState.CONNECTED.value if proxy.enabled else ProxyState.DISCONNECTED.value, "")

        def err(e: BaseException) -> None:
            self.proxy_test_result.emit(False, friendly_error(e))
            self.proxy_state.emit(ProxyState.ERROR.value, friendly_error(e))

        self.run_async(self.gateway.test_proxy(proxy), ok=ok, err=err)

    # ------------------------------------------------------------------ storages
    def create_storage(self, name: str, then: Callable[[int], None] | None = None) -> None:
        def ok(sid: int) -> None:
            self.storages_changed.emit()
            if then:
                then(sid)

        self.run_async(self.storages.create_storage(name), ok=ok)

    def discover(self) -> None:
        async def go() -> int:
            new_ids = await self.storages.discover()
            for sid in [int(r["id"]) for r in self.repos.storages.list()]:
                await self.storages.sync_storage(sid)
            return len(new_ids)

        self.run_async(go(), ok=lambda n: (self.storages_changed.emit(), self.message.emit(f"Found {n} new storage(s) on Telegram." if n else "Storage index refreshed from Telegram.")))

    def delete_cloud_files(self, file_ids: list[int]) -> None:
        self.run_async(self.storages.delete_cloud_files(file_ids, confirmed=True),
                       ok=lambda n: (self.storages_changed.emit(), self.message.emit(f"Deleted {n} file(s) from Telegram. Local files were not touched.")))  # fmt: skip

    def forget_storage(self, storage_id: int) -> None:
        self.storages.forget_storage(storage_id)
        self.storages_changed.emit()

    # ------------------------------------------------------------------ operations
    def _begin(self, kind: str, label: str, storage_id: int | None = None) -> TransferControl | None:
        if self.active is not None:
            self.message.emit("Another operation is already running.")
            return None
        control = TransferControl()
        self.active = ActiveOp(kind, label, control, storage_id)
        self.last_op = self.active
        self._progress.clear()
        self._resumable = ResumeSummary(kind=kind, storage_id=storage_id)
        self.op_started.emit(kind, label)
        self.paused_changed.emit(False)
        return control

    def _finished(self, summary: RunSummary) -> None:
        self.active = None
        self.last_summary = summary
        self.op_finished.emit(summary)
        self.notifications.notify_run(summary)
        self.storages_changed.emit()
        self._refresh_resumable(summary)

    def _refresh_resumable(self, summary: RunSummary | None = None) -> ResumeSummary:
        """Recompute (and publish) what a Resume action would continue."""
        summary = summary or self.last_summary
        self._resumable = ResumeSummary(kind=summary.kind if summary else "backup")
        if summary is not None and summary.storage_id is not None:
            try:
                self._resumable = self.resume_service.summary(summary.storage_id, summary.kind)
            except Exception:
                log.exception("could not compute resumable work")
        self.resumable_changed.emit(self._resumable)
        return self._resumable

    def resumable_summary(self, storage_id: int | None = None, kind: str | None = None) -> ResumeSummary:
        """What is left to do for ``storage_id`` (defaults to the last operation's storage)."""
        op = self.last_op
        sid = storage_id if storage_id is not None else (op.storage_id if op else None)
        if sid is None:
            return ResumeSummary()
        return self.resume_service.summary(int(sid), kind or (op.kind if op else "backup"))

    def _emit_file(self, fid: int, rel: str, state: str, done: float = 0, total: float = 0, speed: float = 0, eta: float | None = None) -> None:
        self.file_event.emit(fid, rel, state, done, total, speed, -1.0 if eta is None else eta)

    def _track_progress(self, fid: int, done: float, total: float) -> tuple[float, float, float, float]:
        """Fold one file's acknowledged bytes into the whole-operation progress.

        Returns ``(done, total, speed, eta)`` for the operation. Only bytes Telegram acknowledged
        ever reach this method: it is fed exclusively by ``on_file_progress``.
        """
        if total or done:
            self._progress[fid] = (done, total)
        op_done = sum(d for d, _ in self._progress.values())
        op_total = sum(t for _, t in self._progress.values())
        return op_done, op_total, 0.0, -1.0

    def _emit_operation_progress(self, fid: int, done: float, total: float, speed: float, eta: float | None) -> None:
        op_done, op_total, _, _ = self._track_progress(fid, done, total)
        self.op_progress.emit(op_done, op_total, speed, -1.0 if eta is None else eta)

    def start_backup(self, storage_id: int, folder: str) -> None:
        control = self._begin("backup", folder, storage_id)
        if control is None:
            return
        names: dict[int, str] = {}
        sizes: dict[int, int] = {}

        async def resolver(info: DuplicateInfo) -> DuplicateDecision:
            fut: concurrent.futures.Future[DuplicateDecision] = concurrent.futures.Future()
            self.ask_duplicate.emit(info, fut)
            return await asyncio.wrap_future(fut)

        def on_start(fid: int, rel: str, size: int, i: int, n: int) -> None:
            names[fid], sizes[fid] = rel, size
            self._emit_file(fid, rel, TransferState.QUEUED.value, 0, size)
            self._emit_operation_progress(fid, 0, size, 0.0, None)

        def on_state(fid: int, st: TransferState) -> None:
            if st == TransferState.COMPLETED:
                return  # the 'done' event below carries the final numbers
            done, total = self._progress.get(fid, (0.0, float(sizes.get(fid, 0))))
            self._emit_file(fid, names.get(fid, ""), st.value, done, total)

        def on_progress(fid: int, snap: ProgressSnapshot) -> None:
            # Real, acknowledged bytes only - the UI never animates on a timer.
            self._emit_file(fid, names.get(fid, ""), TransferState.UPLOADING.value, snap.done, snap.total, snap.speed, snap.eta)
            self._emit_operation_progress(fid, snap.done, snap.total, snap.speed, snap.eta)

        events = BackupEvents(
            on_phase=lambda p: self.op_phase.emit(p),
            on_file_start=on_start,
            on_file_state=on_state,
            on_file_progress=on_progress,
            on_file_done=lambda fid, rel, status, error: self._emit_file(
                fid, rel, {"backed_up": "completed", "skipped": "skipped", "unchanged": "completed", "failed": "failed", "cancelled": "cancelled"}[status], sizes.get(fid, 0), sizes.get(fid, 0)
            ),
        )
        self.run_async(self.backup.run(storage_id, folder, resolver, control, events), ok=self._finished,
                       err=self._op_error)  # fmt: skip

    def _op_error(self, exc: BaseException) -> None:
        """Setup-level failure: the operation never really started, so nothing is 'resumable'."""
        self.active = None
        self.message.emit(friendly_error(exc))
        self._refresh_resumable(self.last_summary)

    def start_restore(self, file_ids: list[int], dest: str) -> None:
        control = self._begin("restore", dest)
        if control is None:
            return
        rows = {fid: self.repos.files.get(fid) for fid in file_ids}

        async def resolver(info: ConflictInfo) -> ConflictDecision:
            fut: concurrent.futures.Future[ConflictDecision] = concurrent.futures.Future()
            self.ask_conflict.emit(info, fut)
            return await asyncio.wrap_future(fut)

        def total(fid: int) -> int:
            r = rows.get(fid)
            return int(r["size"]) if r else 0

        def rel(fid: int) -> str:
            r = rows.get(fid)
            return str(r["rel_path"]) if r else ""

        def on_progress(fid: int, snap: ProgressSnapshot) -> None:
            self._emit_file(fid, rel(fid), TransferState.DOWNLOADING.value, snap.done, snap.total, snap.speed, snap.eta)
            self._emit_operation_progress(fid, snap.done, snap.total, snap.speed, snap.eta)

        events = RestoreEvents(
            on_phase=lambda p: self.op_phase.emit(p),
            on_file_start=lambda fid, r, size, i, n: (
                self._emit_file(fid, r, TransferState.DOWNLOADING.value, 0, size),
                self._emit_operation_progress(fid, 0, size, 0.0, None),
            ),
            on_file_progress=on_progress,
            on_file_done=lambda fid, r, status, error: self._emit_file(
                fid, r, {"restored": "completed", "skipped": "skipped", "failed": "failed", "cancelled": "cancelled"}[status], total(fid), total(fid)
            ),
        )
        self.run_async(self.restore.restore(file_ids, dest, resolver, control, events), ok=self._finished,
                       err=self._op_error)  # fmt: skip

    # ------------------------------------------------------------------ resume after failure
    def resume_last(self) -> bool:
        """Continue the last operation from its last safe checkpoint.

        Returns ``False`` when there is nothing to resume or an operation is already running, so
        the UI never turns a double click into two concurrent uploads.
        """
        op = self.last_op
        if op is None or op.storage_id is None:
            self.message.emit("Nothing to resume yet.")
            return False
        if self.active is not None:
            self.message.emit("Another operation is already running.")
            return False
        if op.kind == "backup":
            folder = self.resume_service.summary(op.storage_id, "backup").local_root or op.label
            self.message.emit("Reconnecting and resuming…")
            self.start_backup(op.storage_id, folder)
            return True
        self.message.emit("Restart the restore from the Restore page.")
        return False

    def pause(self) -> None:
        if self.active:
            self.runner.call_soon(self.active.control.pause)
            self.paused_changed.emit(True)

    def resume(self) -> None:
        if self.active:
            self.runner.call_soon(self.active.control.resume)
            self.paused_changed.emit(False)

    def cancel(self) -> None:
        if self.active:
            self.runner.call_soon(self.active.control.cancel)

    # ------------------------------------------------------------------ settings
    def apply_settings(self, s: AppSettings) -> None:
        self.settings.save(s)
        self.limiter.set_rate(s.speed_bytes_per_second)  # takes effect immediately, mid-transfer
        self.preview.max_download = s.preview_max_mb * 1024 * 1024


def answer_duplicate(fut: concurrent.futures.Future[DuplicateDecision], action: DuplicateAction, apply_all: bool) -> None:
    if not fut.done():
        fut.set_result(DuplicateDecision(action, apply_all))
