"""In-memory Telegram transport used by tests and benchmarks.

It models the parts of MTProto that matter to Teloude: server-side partial upload sessions keyed by a
64-bit file id, ``SaveBigFilePart`` idempotency (re-sending a part overwrites it), session expiry,
flood waits, forum topics and messages with captions. It is NOT a performance model of Telegram: any
throughput measured against it only measures Teloude's own pipeline overhead.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from ..application.ports import (
    AccountInfo,
    RemoteMessage,
    StorageRef,
    TopicInfo,
    UploadSession,
)
from ..domain.errors import (
    InvalidCode,
    InvalidPassword,
    PasswordRequired,
    TopicNotFound,
    TransientNetworkError,
    UploadSessionInvalid,
)
from ..domain.models import ProxySettings, ProxyState, UploadLimits

FailHook = Callable[[str, int], Exception | None]


class SimulatedCrash(BaseException):
    """Stands in for the process dying (power loss, kill -9): not catchable by ``except Exception``."""


@dataclass
class FakeMessage:
    id: int
    topic_id: int
    caption: str
    filename: str
    data: bytes


@dataclass
class FakeChat:
    ref: StorageRef
    topics: dict[int, str] = field(default_factory=dict)
    messages: dict[int, FakeMessage] = field(default_factory=dict)


class FakeGateway:
    def __init__(
        self,
        part_latency: float = 0.0,
        limits: UploadLimits | None = None,
        fail_hook: FailHook | None = None,
        password: str | None = None,
    ):
        self.part_latency = part_latency
        self.limits = limits or UploadLimits()
        self.fail_hook = fail_hook
        self.password = password
        self.sessions: dict[int, dict[int, bytes]] = {}
        self.chats: dict[int, FakeChat] = {}
        self.part_calls = 0
        self.inflight = 0
        self.max_inflight = 0
        self.reconnects = 0
        self.connected = True
        self.authorized = False
        self.proxy = ProxySettings()
        self.proxy_ops: list[str] = []
        self._ids = itertools.count(1000)
        self._topic_ids = itertools.count(2)
        self._chat_ids = itertools.count(-1001000000001, -1)
        self._listener: Callable[[ProxyState, str | None], None] | None = None
        self.call_log: list[str] = []
        self.download_chunk_calls = 0
        self.crash_after_finalize = False

    # -- helpers ----------------------------------------------------------------------------
    def _maybe_fail(self, op: str, call_no: int | None = None) -> None:
        """``call_no`` is this call's own 1-based number, captured when the call started.

        Reading ``self.part_calls`` here instead would make fault injection depend on how the
        workers happen to interleave: four of them can bump the counter between a call starting and
        its hook running, so "fail on the 15th part" would silently never fire.
        """
        if self.fail_hook:
            exc = self.fail_hook(op, self.part_calls if call_no is None else call_no)
            if exc is not None:
                raise exc

    def expire_all_sessions(self) -> None:
        """Simulate Telegram forgetting every partial upload."""
        self.sessions.clear()

    def all_messages(self) -> list[FakeMessage]:
        return [m for c in self.chats.values() for m in c.messages.values()]

    # -- connection / auth ---------------------------------------------------------------
    def set_state_listener(self, cb: Callable[[ProxyState, str | None], None] | None) -> None:
        self._listener = cb

    async def connect(self) -> None:
        self.connected = True
        self.call_log.append("connect")

    async def disconnect(self) -> None:
        self.connected = False

    async def reconnect(self) -> None:
        self._maybe_fail("reconnect")
        self.reconnects += 1
        self.connected = True

    async def is_authorized(self) -> bool:
        return self.authorized

    async def send_code(self, phone: str) -> None:
        self.call_log.append("send_code")

    async def sign_in_code(self, phone: str, code: str) -> None:
        if code != "12345":
            raise InvalidCode("bad code")
        if self.password is not None:
            raise PasswordRequired("2fa")
        self.authorized = True

    async def sign_in_password(self, password: str) -> None:
        if password != self.password:
            raise InvalidPassword("bad password")
        self.authorized = True

    async def sign_out(self) -> None:
        self.authorized = False

    async def account(self) -> AccountInfo:
        return AccountInfo("Test User", premium=False)

    async def get_limits(self) -> UploadLimits:
        return self.limits

    async def apply_proxy(self, proxy: ProxySettings) -> None:
        self.proxy = proxy
        self.proxy_ops.append(f"apply:{proxy.enabled}")
        if self._listener:
            self._listener(ProxyState.CONNECTED if proxy.enabled else ProxyState.DISCONNECTED, None)

    async def test_proxy(self, proxy: ProxySettings) -> None:
        self.proxy_ops.append("test")
        if proxy.host == "bad":
            raise TransientNetworkError("proxy unreachable")

    # -- storage ----------------------------------------------------------------------------
    async def create_storage(self, title: str, uuid: str) -> StorageRef:
        ref = StorageRef(next(self._chat_ids), random.getrandbits(32), title, uuid)
        self.chats[ref.chat_id] = FakeChat(ref)
        return ref

    async def list_storages(self) -> list[StorageRef]:
        return [c.ref for c in self.chats.values()]

    async def create_topic(self, storage: StorageRef, title: str) -> int:
        tid = next(self._topic_ids)
        self.chats[storage.chat_id].topics[tid] = title
        return tid

    async def list_topics(self, storage: StorageRef) -> list[TopicInfo]:
        return [TopicInfo(i, t) for i, t in self.chats[storage.chat_id].topics.items()]

    async def iter_messages(self, storage: StorageRef, topic_id: int | None) -> AsyncIterator[RemoteMessage]:  # type: ignore[override]
        chat = self.chats[storage.chat_id]
        for m in sorted(chat.messages.values(), key=lambda m: -m.id):
            if topic_id is None or m.topic_id == topic_id:
                yield RemoteMessage(m.id, m.topic_id, m.caption, len(m.data))

    # -- upload ----------------------------------------------------------------------------
    def new_upload_session(self, name: str) -> UploadSession:
        fid = random.getrandbits(62)
        self.sessions[fid] = {}
        return UploadSession(fid, name)

    async def validate_upload_session(self, session: UploadSession) -> bool:
        """Faithful model of the server-side session table.

        MTProto has no cheap "do you still know this file id?" query, so real adapters can only
        check connectivity. The fake *can* answer exactly, which is what lets the tests exercise
        the expired-session resume path end to end.
        """
        self._maybe_fail("validate_session")
        return session.file_id in self.sessions

    async def upload_part(self, session: UploadSession, index: int, total_parts: int, data: bytes) -> None:
        self.part_calls += 1
        call_no = self.part_calls  # this call's own number: other workers must not change it
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            # Always yield: a network call always hands control back to the event loop, even one
            # that completes instantly. With part_latency == 0 this is what keeps the parts of
            # concurrent workers genuinely overlapping (max_inflight > 1).
            await asyncio.sleep(self.part_latency)
            self._maybe_fail("upload_part", call_no)
            if not self.connected:
                raise TransientNetworkError("not connected")
            if session.file_id not in self.sessions:
                if self.sessions.get(session.file_id) is None and session.file_id not in self.sessions:
                    # Telegram accepts parts for an id it has never seen (it simply starts a new one)
                    self.sessions[session.file_id] = {}
            self.sessions[session.file_id][index] = bytes(data)
        finally:
            self.inflight -= 1

    async def finalize_upload(
        self, storage: StorageRef, topic_id: int, session: UploadSession, total_parts: int, filename: str, caption: str
    ) -> int:
        self._maybe_fail("finalize")
        chat = self.chats[storage.chat_id]
        if topic_id not in chat.topics:
            raise TopicNotFound(str(topic_id))
        parts = self.sessions.get(session.file_id, {})
        if len(parts) != total_parts or any(i not in parts for i in range(total_parts)):
            raise UploadSessionInvalid("FILE_PART_X_MISSING")
        data = b"".join(parts[i] for i in range(total_parts))
        mid = next(self._ids)
        chat.messages[mid] = FakeMessage(mid, topic_id, caption, filename, data)
        self.sessions.pop(session.file_id, None)
        if self.crash_after_finalize:
            self.crash_after_finalize = False
            raise SimulatedCrash("died after Telegram accepted the message")
        return mid

    async def find_volume(self, storage: StorageRef, topic_id: int, sha256: str, volume_index: int) -> int | None:
        from ..application.metadata import decode_caption

        async for m in self.iter_messages(storage, topic_id):
            meta = decode_caption(m.caption)
            if meta and meta.sha256 == sha256 and meta.volume_index == volume_index:
                return m.message_id
        return None

    # -- download -----------------------------------------------------------------------------
    async def iter_download(
        self, storage: StorageRef, message_id: int, offset: int = 0, chunk_size: int = 512 * 1024
    ) -> AsyncIterator[bytes]:
        msg = self.chats[storage.chat_id].messages[message_id]
        pos = offset
        while pos < len(msg.data):
            self.download_chunk_calls += 1
            self._maybe_fail("download")
            if self.part_latency:
                await asyncio.sleep(self.part_latency)
            yield msg.data[pos : pos + chunk_size]
            pos += chunk_size

    async def download_thumbnail(self, storage: StorageRef, message_id: int) -> bytes | None:
        return None

    async def delete_messages(self, storage: StorageRef, message_ids: list[int]) -> None:
        chat = self.chats[storage.chat_id]
        for mid in message_ids:
            chat.messages.pop(mid, None)


def now() -> float:
    return time.monotonic()
