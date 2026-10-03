"""Telethon adapter (driven through a recording fake client), secret stores and settings."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime

import pytest
from telethon import errors, functions, types
from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyIntermediate,
    ConnectionTcpMTProxyRandomizedIntermediate,
)

from app.application.ports import StorageRef
from app.application.settings import SettingsService
from app.domain.errors import (
    ProxyUnsupported,
    RateLimited,
    TransientNetworkError,
    UploadSessionInvalid,
)
from app.domain.models import ProxySettings
from app.infrastructure.db import open_database
from app.infrastructure.repositories import Repos
from app.infrastructure.secrets import (
    InsecureFileSecretStore,
    MemorySecretStore,
    dpapi_protect,
    dpapi_unprotect,
)
from app.infrastructure.telethon_gateway import TelethonGateway, parse_proxy_secret

NOW = datetime(2026, 1, 1)


class RecordingClient:
    def __init__(self, session, api_id, api_hash, **kwargs):
        self.kwargs = kwargs
        self.requests: list = []
        self.responses: list = []
        self.raise_next: BaseException | None = None
        self.responder = None

    async def __call__(self, request):
        self.requests.append(request)
        if self.raise_next:
            e, self.raise_next = self.raise_next, None
            raise e
        if self.responder:
            return self.responder(request)
        return self.responses.pop(0) if self.responses else True

    def is_connected(self):
        return True


def make_gateway(proxy=ProxySettings()):
    holder = {}

    def factory(*a, **k):
        holder["client"] = RecordingClient(*a, **k)
        return holder["client"]

    gw = TelethonGateway(lambda: (1, "h" * 32), MemorySecretStore(), lambda: proxy, factory)
    gw._ensure_client()
    return gw, holder["client"]


REF = StorageRef(555, 99, "Teloude - X", "u")


def updates(*items):
    return types.Updates(updates=list(items), users=[], chats=[], date=NOW, seq=0)


# ------------------------------------------------------------------ proxy
def test_proxy_secret_kinds():
    plain = "0123456789abcdef0123456789abcdef"
    assert parse_proxy_secret(plain) == (ConnectionTcpMTProxyIntermediate, plain)
    cls, sec = parse_proxy_secret("dd" + plain)
    assert cls is ConnectionTcpMTProxyRandomizedIntermediate and sec == "dd" + plain
    with pytest.raises(ProxyUnsupported, match="fake-TLS"):
        parse_proxy_secret("ee" + plain + "676f6f676c652e636f6d")  # Telethon would silently strip 'ee' and fail
    with pytest.raises(ProxyUnsupported):
        parse_proxy_secret("not a secret!!")
    with pytest.raises(ProxyUnsupported):
        parse_proxy_secret("abcd")


def test_proxy_is_passed_to_the_single_shared_client():
    secret = "0123456789abcdef0123456789abcdef"
    gw, client = make_gateway(ProxySettings("proxy.example.com", 8443, secret, True))
    assert client.kwargs["proxy"] == ("proxy.example.com", 8443, secret)
    assert client.kwargs["connection"] is ConnectionTcpMTProxyIntermediate
    gw2, client2 = make_gateway(ProxySettings("p", 1, secret, False))  # disabled -> direct
    assert "proxy" not in client2.kwargs


# ------------------------------------------------------------------ request construction
async def test_create_storage_makes_private_forum_supergroup():
    gw, c = make_gateway()
    chan = types.Channel(id=555, title="Teloude - X", photo=types.ChatPhotoEmpty(), date=NOW, access_hash=99, megagroup=True, forum=True)
    c.responses = [updates() and types.Updates(updates=[], users=[], chats=[chan], date=NOW, seq=0)]
    ref = await gw.create_storage("Teloude - X", "uuid-1")
    req = c.requests[0]
    assert isinstance(req, functions.channels.CreateChannelRequest)
    assert req.megagroup and req.forum and not req.broadcast and "uuid-1" in req.about
    assert (ref.chat_id, ref.access_hash, ref.uuid) == (555, 99, "uuid-1")


async def test_create_topic_returns_service_message_id():
    gw, c = make_gateway()
    msg = types.Message(id=42, peer_id=types.PeerChannel(555), date=NOW, message="")
    c.responses = [updates(types.UpdateNewChannelMessage(msg, pts=1, pts_count=1))]
    assert await gw.create_topic(REF, "Photos / 2026") == 42
    req = c.requests[0]
    assert isinstance(req, functions.messages.CreateForumTopicRequest) and req.title == "Photos / 2026"
    assert isinstance(req.peer, types.InputPeerChannel) and req.peer.channel_id == 555


async def test_big_vs_small_part_requests_and_finalize_media():
    gw, c = make_gateway()
    sess = gw.new_upload_session("f.bin")
    await gw.upload_part(sess, 3, 30, b"x" * 10)  # > 20 parts -> "big file" API
    assert isinstance(c.requests[-1], functions.upload.SaveBigFilePartRequest)
    assert (c.requests[-1].file_part, c.requests[-1].file_total_parts) == (3, 30)
    c.responses = [updates(types.UpdateMessageID(id=777, random_id=0))]
    c.requests.clear()
    # small file: SaveFilePart + md5 over the ordered content
    small = gw.new_upload_session("s.txt")
    await gw.upload_part(small, 1, 2, b"world")
    await gw.upload_part(small, 0, 2, b"hello")
    assert all(isinstance(r, functions.upload.SaveFilePartRequest) for r in c.requests)
    c.requests.clear()

    c.responder = lambda r: updates(types.UpdateMessageID(id=777, random_id=r.random_id))
    mid = await gw.finalize_upload(REF, 9, small, 2, "s.txt", "cap")
    req = c.requests[0]
    assert mid == 777 and isinstance(req, functions.messages.SendMediaRequest)
    assert isinstance(req.media.file, types.InputFile) and req.media.file.md5_checksum == hashlib.md5(b"helloworld").hexdigest()  # noqa: S324
    assert req.media.force_file and req.message == "cap"
    assert req.reply_to.reply_to_msg_id == 9 and req.reply_to.top_msg_id == 9  # lands in the right topic
    assert req.media.attributes[0].file_name == "s.txt"
    big = gw.new_upload_session("b.bin")
    c.requests.clear()
    await gw.finalize_upload(REF, 9, big, 30, "b.bin", "c")
    assert isinstance(c.requests[0].media.file, types.InputFileBig) and c.requests[0].media.file.parts == 30


async def test_small_file_resumed_after_restart_restarts_instead_of_sending_bad_md5():
    gw, c = make_gateway()
    lost = gw.new_upload_session("s.txt")  # buffer empty: process restarted between parts and finalize
    with pytest.raises(UploadSessionInvalid):
        await gw.finalize_upload(REF, 9, lost, 3, "s.txt", "c")


@pytest.mark.parametrize(
    "exc,expected",
    [
        (errors.FilePartMissingError(None, 3), UploadSessionInvalid),
        (errors.FileIdInvalidError(None), UploadSessionInvalid),
        (errors.FloodWaitError(None, 7), RateLimited),
        (ConnectionError("reset"), TransientNetworkError),
        (TimeoutError(), TransientNetworkError),
        (errors.ServerError(None, "boom"), TransientNetworkError),
    ],
)
async def test_errors_are_mapped_to_domain_errors(exc, expected):
    gw, c = make_gateway()
    c.raise_next = exc
    with pytest.raises(expected):
        await gw.upload_part(gw.new_upload_session("x"), 0, 40, b"x")


async def test_flood_wait_seconds_preserved():
    gw, c = make_gateway()
    c.raise_next = errors.FloodWaitError(None, 11)
    with pytest.raises(RateLimited) as ei:
        await gw.upload_part(gw.new_upload_session("x"), 0, 40, b"x")
    assert ei.value.seconds == 11


# ------------------------------------------------------------------ secrets & settings
def test_insecure_store_roundtrip_is_dev_only_and_validates_names(tmp_path):
    s = InsecureFileSecretStore(tmp_path)
    s.set("telegram_session", "value")
    assert s.get("telegram_session") == "value" and not list(tmp_path.glob("*.tmp"))
    s.delete("telegram_session")
    assert s.get("telegram_session") is None
    with pytest.raises(ValueError):
        s.set("../evil", "x")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only; run on Windows via scripts/windows_verify.py")
def test_dpapi_roundtrip_and_no_plaintext_on_disk(tmp_path):
    from app.infrastructure.secrets import DpapiSecretStore

    blob = dpapi_protect(b"top secret")
    assert b"top secret" not in blob and dpapi_unprotect(blob) == b"top secret"
    store = DpapiSecretStore(tmp_path)
    store.set("telegram_session", "SESSION-STRING-123")
    assert b"SESSION-STRING-123" not in (tmp_path / "telegram_session.dpapi").read_bytes()
    assert store.get("telegram_session") == "SESSION-STRING-123"


def test_settings_service_keeps_secrets_out_of_the_database(tmp_path, monkeypatch):
    monkeypatch.delenv("TELOUDE_API_ID", raising=False)
    monkeypatch.delenv("TELOUDE_API_HASH", raising=False)
    # inject_credentials.py may have written app/teloude_api.json for a release build; this test is
    # about the application itself carrying nothing, so no build file may leak in here.
    monkeypatch.setattr("app.application.settings.bundled_api_credentials", lambda: None)
    db = open_database(tmp_path / "s.db")
    secrets = MemorySecretStore()
    svc = SettingsService(Repos.create(db).settings, secrets)
    assert svc.get_api_credentials() is None  # nothing hard-coded
    svc.set_api_credentials(777, "apihash-secret-value")
    svc.set_proxy(ProxySettings("h", 443, "ab" * 16, True))
    dump = " ".join(str(v) for r in db.query("SELECT key,value FROM settings") for v in r)
    assert "apihash-secret-value" not in dump and "abab" not in dump
    assert svc.get_api_credentials() == (777, "apihash-secret-value")
    assert svc.get_proxy() == ProxySettings("h", 443, "ab" * 16, True)
    monkeypatch.setenv("TELOUDE_API_ID", "9")
    monkeypatch.setenv("TELOUDE_API_HASH", "envhash")
    assert svc.get_api_credentials() == (9, "envhash")  # environment wins
    s = svc.load()
    s.speed_bytes_per_second, s.concurrency = 5 * 1024 * 1024, 99
    svc.save(s)
    assert svc.load().speed_bytes_per_second == 5 * 1024 * 1024 and svc.load().concurrency == 8  # clamped
