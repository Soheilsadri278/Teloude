"""Abstractions the application layer depends on. Telegram sits behind ``TelegramGateway`` so every
orchestration path can be tested with the in-memory fake in ``app.testing.fake_gateway``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..domain.models import ProxySettings, ProxyState, UploadLimits


@dataclass(frozen=True)
class StorageRef:
    chat_id: int
    access_hash: int | None
    title: str
    uuid: str


@dataclass(frozen=True)
class AccountInfo:
    display_name: str
    premium: bool = False


@dataclass(frozen=True)
class TopicInfo:
    id: int
    title: str


@dataclass(frozen=True)
class RemoteMessage:
    message_id: int
    topic_id: int | None
    caption: str
    size: int


@dataclass
class UploadSession:
    """A server-side partial upload (random 64-bit ``file_id`` + parts already sent)."""

    file_id: int
    name: str
    extra: dict = field(default_factory=dict)  # adapter-private scratch (e.g. small-file md5 buffer)


@runtime_checkable
class TelegramGateway(Protocol):
    # --- connection / auth -------------------------------------------------------------
    def set_state_listener(self, cb: Callable[[ProxyState, str | None], None] | None) -> None: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def reconnect(self) -> None: ...
    async def is_authorized(self) -> bool: ...
    async def send_code(self, phone: str) -> None: ...
    async def sign_in_code(self, phone: str, code: str) -> None: ...  # raises PasswordRequired
    async def sign_in_password(self, password: str) -> None: ...
    async def sign_out(self) -> None: ...
    async def account(self) -> AccountInfo: ...
    async def get_limits(self) -> UploadLimits: ...
    # --- proxy ---------------------------------------------------------------------------
    async def apply_proxy(self, proxy: ProxySettings) -> None: ...
    async def test_proxy(self, proxy: ProxySettings) -> None: ...
    # --- storage -------------------------------------------------------------------------
    async def create_storage(self, title: str, uuid: str) -> StorageRef: ...
    async def list_storages(self) -> list[StorageRef]: ...
    async def create_topic(self, storage: StorageRef, title: str) -> int: ...
    async def list_topics(self, storage: StorageRef) -> list[TopicInfo]: ...
    def iter_messages(self, storage: StorageRef, topic_id: int | None) -> AsyncIterator[RemoteMessage]: ...
    # --- upload --------------------------------------------------------------------------
    def new_upload_session(self, name: str) -> UploadSession: ...
    async def validate_upload_session(self, session: UploadSession) -> bool: ...
    async def upload_part(self, session: UploadSession, index: int, total_parts: int, data: bytes) -> None: ...
    async def finalize_upload(
        self, storage: StorageRef, topic_id: int, session: UploadSession, total_parts: int, filename: str, caption: str
    ) -> int: ...
    async def find_volume(self, storage: StorageRef, topic_id: int, sha256: str, volume_index: int) -> int | None: ...
    # --- download ------------------------------------------------------------------------
    def iter_download(
        self, storage: StorageRef, message_id: int, offset: int = 0, chunk_size: int = 512 * 1024
    ) -> AsyncIterator[bytes]: ...
    async def download_thumbnail(self, storage: StorageRef, message_id: int) -> bytes | None: ...
    async def delete_messages(self, storage: StorageRef, message_ids: list[int]) -> None: ...


class SecretStore(Protocol):
    """Protected storage for secrets (Telegram session, API hash, proxy secret)."""

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...
