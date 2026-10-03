"""Telethon (MTProto) implementation of ``TelegramGateway``.

NOT the Bot API: this logs in as the user's own account. Verified against Telethon's TL layer at
build time by ``tests/integration/test_telethon_adapter.py`` (request construction) - but it has NOT been
exercised against live Telegram servers in CI; see docs/VERIFICATION.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

from telethon import TelegramClient, errors, functions, types
from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyIntermediate,
    ConnectionTcpMTProxyRandomizedIntermediate,
)
from telethon.sessions import StringSession

from ..application.ports import (
    AccountInfo,
    RemoteMessage,
    SecretStore,
    StorageRef,
    TopicInfo,
    UploadSession,
)
from ..domain.errors import (
    InvalidCode,
    InvalidPassword,
    MissingApiCredentials,
    PasswordRequired,
    PermanentTransferError,
    ProxyUnsupported,
    RateLimited,
    TopicNotFound,
    TransientNetworkError,
    UploadSessionInvalid,
)
from ..domain.models import KIB, ProxySettings, ProxyState, UploadLimits
from ..domain.redact import register_secret
from .config import APP_NAME, APP_VERSION

log = logging.getLogger("teloude.telegram")

ABOUT_MARKER = "teloude:storage:v1:"
STORAGE_PREFIX = "Teloude - "
SMALL_FILE_MAX_PARTS = 20  # <= 10 MiB with 512 KiB parts: Telegram wants SaveFilePart (not "big") here
SESSION_SECRET = "telegram_session"
_SESSION_ERRORS = (
    errors.FilePartMissingError,
    errors.FileIdInvalidError,
    errors.FilePartsInvalidError,
    errors.FilePart0MissingError,
)


def parse_proxy_secret(secret: str) -> tuple[type, str]:
    """Return (Telethon connection class, secret) or raise ProxyUnsupported.

    Telethon speaks plain and 'dd' (randomized padding) MTProxy secrets. It cannot speak 'ee'
    fake-TLS proxies and would silently strip the prefix and fail, so we reject them explicitly."""
    s = secret.strip()
    raw: bytes | None = None
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        try:
            raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        except Exception:
            raw = None
    if not raw:
        raise ProxyUnsupported("The proxy secret is not valid hex or base64.")
    if raw[0] == 0xEE:
        raise ProxyUnsupported(
            "This is a fake-TLS ('ee') proxy secret. Telethon cannot connect through fake-TLS proxies; "
            "use a proxy with a plain or 'dd' secret."
        )
    hex_secret = raw.hex()
    if raw[0] == 0xDD and len(raw) == 17:
        return ConnectionTcpMTProxyRandomizedIntermediate, hex_secret
    if len(raw) == 16:
        return ConnectionTcpMTProxyIntermediate, hex_secret
    raise ProxyUnsupported("Unsupported proxy secret length.")


def _json_value(v: Any) -> Any:
    if isinstance(v, types.JsonNumber):
        return v.value
    if isinstance(v, types.JsonString):
        return v.value
    if isinstance(v, types.JsonBool):
        return v.value
    if isinstance(v, types.JsonArray):
        return [_json_value(x) for x in v.value]
    if isinstance(v, types.JsonObject):
        return {e.key: _json_value(e.value) for e in v.value}
    return None


class TelethonGateway:
    def __init__(
        self,
        credentials: Callable[[], tuple[int, str] | None],
        secrets: SecretStore,
        proxy: Callable[[], ProxySettings],
        client_factory: Callable[..., Any] = TelegramClient,
    ):
        self._credentials = credentials
        self._secrets = secrets
        self._proxy_provider = proxy
        self._factory = client_factory
        self._client: Any = None
        self._listener: Callable[[ProxyState, str | None], None] | None = None
        self._phone_hash: str | None = None
        self._limits: UploadLimits | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ plumbing
    def set_state_listener(self, cb: Callable[[ProxyState, str | None], None] | None) -> None:
        self._listener = cb

    def _emit(self, state: ProxyState, detail: str | None = None) -> None:
        if self._listener:
            try:
                self._listener(state, detail)
            except Exception:
                log.exception("state listener failed")

    def _proxy_active(self) -> bool:
        p = self._proxy_provider()
        return p.enabled and p.is_complete()

    def _make_client(self, session: str, api: tuple[int, str], proxy: ProxySettings) -> Any:
        kwargs: dict[str, Any] = dict(
            connection_retries=3, retry_delay=1, request_retries=3, auto_reconnect=True,
            flood_sleep_threshold=60, timeout=20, device_model=APP_NAME, app_version=APP_VERSION,
        )  # fmt: skip
        if proxy.enabled and proxy.is_complete():
            conn, secret = parse_proxy_secret(proxy.secret)
            register_secret(proxy.secret)
            kwargs.update(connection=conn, proxy=(proxy.host.strip(), int(proxy.port), secret))
        return self._factory(StringSession(session), api[0], api[1], **kwargs)

    def _ensure_client(self) -> Any:
        if self._client is None:
            api = self._credentials()
            if api is None:
                raise MissingApiCredentials("Telegram API credentials are not configured")
            session = self._secrets.get(SESSION_SECRET) or ""
            register_secret(session)
            self._client = self._make_client(session, api, self._proxy_provider())
        return self._client

    def _save_session(self) -> None:
        if self._client is not None:
            s = self._client.session.save()
            if s:
                register_secret(s)
                self._secrets.set(SESSION_SECRET, s)

    @staticmethod
    def _translate(exc: BaseException) -> BaseException:
        """Map Telethon/OS errors to domain errors (never leaks secrets: only the class name)."""
        if isinstance(exc, errors.FloodWaitError):
            return RateLimited(exc.seconds)
        if isinstance(exc, errors.SlowModeWaitError):
            return RateLimited(exc.seconds)
        if isinstance(exc, _SESSION_ERRORS):
            return UploadSessionInvalid(type(exc).__name__)
        msg = getattr(exc, "message", "") or ""
        if isinstance(exc, errors.RPCError) and ("TOPIC_" in msg or "TOPIC_DELETED" in msg):
            return TopicNotFound(msg)
        if isinstance(exc, (errors.ServerError, errors.TimedOutError, errors.RpcCallFailError)):
            return TransientNetworkError(type(exc).__name__)
        if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError, EOFError)):
            return TransientNetworkError(type(exc).__name__)
        return exc

    async def _call(self, request: Any) -> Any:
        client = self._ensure_client()
        try:
            return await client(request)
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            mapped = self._translate(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    def _peer(self, s: StorageRef) -> types.InputPeerChannel:
        return types.InputPeerChannel(s.chat_id, s.access_hash or 0)

    def _channel(self, s: StorageRef) -> types.InputChannel:
        return types.InputChannel(s.chat_id, s.access_hash or 0)

    # ------------------------------------------------------------------ connection
    async def connect(self) -> None:
        async with self._lock:
            client = self._ensure_client()
            using_proxy = self._proxy_active()
            if using_proxy:
                self._emit(ProxyState.CONNECTING)
            try:
                await asyncio.wait_for(client.connect(), 40)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if using_proxy:
                    self._emit(ProxyState.ERROR, type(exc).__name__)
                mapped = self._translate(exc)
                raise mapped from exc
            self._emit(ProxyState.CONNECTED if using_proxy else ProxyState.DISCONNECTED)

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._save_session()
            finally:
                await self._client.disconnect()
        self._emit(ProxyState.DISCONNECTED)

    async def reconnect(self) -> None:
        client = self._ensure_client()
        if client.is_connected():
            try:  # cheap liveness probe first
                await asyncio.wait_for(client(functions.PingRequest(ping_id=random.getrandbits(62))), 10)
                return
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
        try:
            await client.disconnect()
            await asyncio.wait_for(client.connect(), 40)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            if self._proxy_active():
                self._emit(ProxyState.ERROR, type(exc).__name__)
            raise self._translate(exc) from exc
        if self._proxy_active():
            self._emit(ProxyState.CONNECTED)

    async def is_authorized(self) -> bool:
        client = self._ensure_client()
        if not client.is_connected():
            await self.connect()
        return bool(await client.is_user_authorized())

    # ------------------------------------------------------------------ login
    async def send_code(self, phone: str) -> None:
        client = self._ensure_client()
        if not client.is_connected():
            await self.connect()
        try:
            sent = await client.send_code_request(phone)
        except errors.PhoneNumberInvalidError as exc:
            raise InvalidCode("That phone number is not valid.") from exc
        except BaseException as exc:
            raise self._translate(exc) from exc
        self._phone_hash = sent.phone_code_hash

    async def sign_in_code(self, phone: str, code: str) -> None:
        client = self._ensure_client()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=self._phone_hash)
        except errors.SessionPasswordNeededError as exc:
            raise PasswordRequired("two-step verification password required") from exc
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError, errors.PhoneCodeEmptyError) as exc:
            raise InvalidCode("The code is wrong or has expired.") from exc
        except BaseException as exc:
            raise self._translate(exc) from exc
        self._save_session()

    async def sign_in_password(self, password: str) -> None:
        client = self._ensure_client()
        try:
            await client.sign_in(password=password)
        except errors.PasswordHashInvalidError as exc:
            raise InvalidPassword("Wrong password.") from exc
        except BaseException as exc:
            raise self._translate(exc) from exc
        self._save_session()

    async def sign_out(self) -> None:
        if self._client is not None:
            try:
                await self._client.log_out()
            except Exception:
                log.warning("log_out failed; local session removed anyway")
            await self._client.disconnect()
            self._client = None
        self._secrets.delete(SESSION_SECRET)

    async def account(self) -> AccountInfo:
        me = await self._ensure_client().get_me()
        name = " ".join(x for x in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if x) or "Telegram user"
        return AccountInfo(name, bool(getattr(me, "premium", False)))

    async def get_limits(self) -> UploadLimits:
        """Limits come from the account (premium?) and server app-config - not from hard-coded history."""
        if self._limits is not None:
            return self._limits
        premium = (await self.account()).premium
        max_parts = 8000 if premium else 4000
        caption = 2048 if premium else 1024
        try:
            cfg = _json_value((await self._call(functions.help.GetAppConfigRequest(hash=0))).config)
            key = "premium" if premium else "default"
            max_parts = int(cfg.get(f"upload_max_fileparts_{key}", max_parts))
            caption = int(cfg.get(f"caption_length_limit_{key}", caption))
        except Exception as exc:
            log.info("using fallback upload limits (%s)", type(exc).__name__)
        self._limits = UploadLimits(part_size=512 * KIB, max_parts=max_parts, caption_limit=caption, premium=premium)
        return self._limits

    # ------------------------------------------------------------------ proxy
    async def apply_proxy(self, proxy: ProxySettings) -> None:
        """Rebuild the one shared client so EVERY operation (auth, upload, download, search) uses it."""
        async with self._lock:
            session = ""
            if self._client is not None:
                self._save_session()
                session = self._client.session.save()
                await self._client.disconnect()
                self._client = None
            if not session:
                session = self._secrets.get(SESSION_SECRET) or ""
            self._limits = None
        api = self._credentials()
        if api is not None:
            self._client = self._make_client(session, api, proxy)
        if proxy.enabled and proxy.is_complete():
            self._emit(ProxyState.CONNECTING)
        else:
            self._emit(ProxyState.DISCONNECTED)
        if api is not None and self._client is not None:
            await self.connect()

    async def test_proxy(self, proxy: ProxySettings) -> None:
        """Real end-to-end check: TCP to proxy + obfuscated handshake + MTProto auth-key exchange."""
        if not proxy.is_complete():
            raise ProxyUnsupported("Enter server, port and secret first.")
        api = self._credentials() or (1, "0" * 32)  # the handshake does not need real API credentials
        client = self._make_client("", api, ProxySettings(proxy.host, proxy.port, proxy.secret, True))
        try:
            await asyncio.wait_for(client.connect(), 25)
            if not client.is_connected():
                raise TransientNetworkError("not connected")
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, ProxyUnsupported)):
                raise
            raise self._translate(exc) from exc
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------ storages & topics
    async def create_storage(self, title: str, uuid: str) -> StorageRef:
        res = await self._call(
            functions.channels.CreateChannelRequest(title=title, about=f"{ABOUT_MARKER}{uuid}", megagroup=True, forum=True)
        )
        chan = next(c for c in res.chats if isinstance(c, types.Channel))
        ref = StorageRef(chan.id, chan.access_hash, title, uuid)
        if not getattr(chan, "forum", False):
            await self._call(functions.channels.ToggleForumRequest(channel=self._channel(ref), enabled=True, tabs=False))
        return ref

    async def list_storages(self) -> list[StorageRef]:
        client = self._ensure_client()
        out: list[StorageRef] = []
        async for d in client.iter_dialogs():
            ent = d.entity
            if not (isinstance(ent, types.Channel) and ent.megagroup and getattr(ent, "forum", False)):
                continue
            if not (ent.title or "").startswith(STORAGE_PREFIX) or getattr(ent, "left", False):
                continue
            ref = StorageRef(ent.id, ent.access_hash, ent.title, "")
            try:
                full = await self._call(functions.channels.GetFullChannelRequest(self._channel(ref)))
            except Exception:
                continue
            about = full.full_chat.about or ""
            if ABOUT_MARKER in about:
                uuid = about.split(ABOUT_MARKER, 1)[1].split()[0]
                out.append(StorageRef(ent.id, ent.access_hash, ent.title, uuid))
        return out

    async def create_topic(self, storage: StorageRef, title: str) -> int:
        rid = random.getrandbits(62)
        res = await self._call(
            functions.messages.CreateForumTopicRequest(peer=self._peer(storage), title=title, random_id=rid)
        )
        for u in getattr(res, "updates", []):
            if isinstance(u, (types.UpdateNewChannelMessage, types.UpdateNewMessage)):
                return int(u.message.id)
        raise PermanentTransferError("Telegram did not return the new topic id")

    async def list_topics(self, storage: StorageRef) -> list[TopicInfo]:
        topics: list[TopicInfo] = []
        offset_date, offset_id, offset_topic = None, 0, 0
        while True:
            res = await self._call(
                functions.messages.GetForumTopicsRequest(
                    peer=self._peer(storage), offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100
                )
            )
            batch = [t for t in res.topics if isinstance(t, types.ForumTopic)]
            topics += [TopicInfo(t.id, t.title) for t in batch]
            if len(res.topics) < 100 or not batch:
                return topics
            last = batch[-1]
            offset_date, offset_id, offset_topic = last.date, last.top_message, last.id

    async def iter_messages(self, storage: StorageRef, topic_id: int | None) -> AsyncIterator[RemoteMessage]:  # type: ignore[override]
        client = self._ensure_client()
        kwargs: dict[str, Any] = {"reply_to": topic_id} if topic_id else {}
        try:
            async for m in client.iter_messages(self._peer(storage), **kwargs):
                doc = getattr(m, "document", None)
                if doc is not None:
                    yield RemoteMessage(m.id, topic_id, m.message or "", int(doc.size))
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                raise
            raise self._translate(exc) from exc

    # ------------------------------------------------------------------ upload
    def new_upload_session(self, name: str) -> UploadSession:
        return UploadSession(random.getrandbits(62) + 1, name)

    async def validate_upload_session(self, session: UploadSession) -> bool:
        """Best-effort check that a persisted partial upload is still worth resuming.

        MTProto deliberately has no cheap "do you still know this file id?" request: re-sending a
        part for an unknown ``file_id`` silently starts a new server-side session, and the truth
        only comes back at ``messages.sendMedia`` time (``FILE_PART_X_MISSING``). So this method
        does the two things that *are* checkable - the session id is well formed and the client is
        really connected (reconnecting if necessary, which also re-establishes the proxy tunnel) -
        and returns True. If Telegram has in fact forgotten the upload, ``finalize_upload`` raises
        ``UploadSessionInvalid`` and the pipeline restarts it with a fresh session. That path is
        covered by tests and is the authoritative answer; pretending otherwise here would be worse.
        """
        if not session or not session.file_id:
            return False
        client = self._ensure_client()
        if not client.is_connected():
            await self.reconnect()
        return bool(client.is_connected())

    async def upload_part(self, session: UploadSession, index: int, total_parts: int, data: bytes) -> None:
        if total_parts > SMALL_FILE_MAX_PARTS:
            req: Any = functions.upload.SaveBigFilePartRequest(session.file_id, index, total_parts, data)
        else:
            req = functions.upload.SaveFilePartRequest(session.file_id, index, data)
            session.extra.setdefault("buf", {})[index] = data  # needed for the md5 of small files
        if not await self._call(req):
            raise TransientNetworkError("Telegram rejected the part")

    async def finalize_upload(
        self, storage: StorageRef, topic_id: int, session: UploadSession, total_parts: int, filename: str, caption: str
    ) -> int:
        if total_parts > SMALL_FILE_MAX_PARTS:
            handle: Any = types.InputFileBig(session.file_id, total_parts, filename)
        else:
            buf: dict[int, bytes] = session.extra.get("buf", {})
            if len(buf) != total_parts:  # resumed after a restart: md5 cannot be computed -> restart (<=10 MiB)
                raise UploadSessionInvalid("small-file buffer lost")
            md5 = hashlib.md5(b"".join(buf[i] for i in range(total_parts))).hexdigest()  # noqa: S324 - protocol-mandated
            handle = types.InputFile(session.file_id, total_parts, filename, md5)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        media = types.InputMediaUploadedDocument(
            file=handle, mime_type=mime, attributes=[types.DocumentAttributeFilename(filename)], force_file=True
        )
        rid = random.getrandbits(62)
        res = await self._call(
            functions.messages.SendMediaRequest(
                peer=self._peer(storage), media=media, message=caption, random_id=rid,
                reply_to=types.InputReplyToMessage(reply_to_msg_id=topic_id, top_msg_id=topic_id),
            )
        )  # fmt: skip
        for u in getattr(res, "updates", []):
            if isinstance(u, types.UpdateMessageID) and u.random_id == rid:
                return int(u.id)
        for u in getattr(res, "updates", []):
            if isinstance(u, types.UpdateNewChannelMessage):
                return int(u.message.id)
        raise PermanentTransferError("Telegram did not return the message id")

    async def find_volume(self, storage: StorageRef, topic_id: int, sha256: str, volume_index: int) -> int | None:
        from ..application.metadata import decode_caption

        n = 0
        async for m in self.iter_messages(storage, topic_id):
            meta = decode_caption(m.caption)
            if meta and meta.sha256 == sha256 and meta.volume_index == volume_index:
                return m.message_id
            n += 1
            if n >= 200:  # an interrupted finalize is always among the most recent messages
                break
        return None

    # ------------------------------------------------------------------ download / delete
    async def iter_download(
        self, storage: StorageRef, message_id: int, offset: int = 0, chunk_size: int = 512 * KIB
    ) -> AsyncIterator[bytes]:
        client = self._ensure_client()
        try:
            msg = await client.get_messages(self._peer(storage), ids=message_id)
            if msg is None or getattr(msg, "document", None) is None:
                raise PermanentTransferError("the Telegram message no longer exists (deleted in Telegram?)")
            async for chunk in client.iter_download(
                msg.document, offset=offset, request_size=chunk_size, file_size=msg.document.size
            ):
                yield bytes(chunk)
        except PermanentTransferError:
            raise
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                raise
            raise self._translate(exc) from exc

    async def download_thumbnail(self, storage: StorageRef, message_id: int) -> bytes | None:
        client = self._ensure_client()
        try:
            msg = await client.get_messages(self._peer(storage), ids=message_id)
            if msg is None:
                return None
            data = await client.download_media(msg, file=bytes, thumb=-1)
            return data or None
        except Exception as exc:
            log.info("thumbnail unavailable (%s)", type(exc).__name__)
            return None

    async def delete_messages(self, storage: StorageRef, message_ids: list[int]) -> None:
        client = self._ensure_client()
        try:
            await client.delete_messages(self._peer(storage), message_ids)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise self._translate(exc) from exc
