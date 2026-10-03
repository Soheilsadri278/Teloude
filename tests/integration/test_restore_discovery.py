from __future__ import annotations

import os

import pytest

from app.application.control import TransferControl
from app.application.preview import PreviewService
from app.application.restore import (
    CHUNK,
    ConflictDecision,
    RestoreService,
)
from app.application.search import SearchService
from app.application.storage_service import StorageService
from app.domain.errors import TransientNetworkError
from app.domain.models import ConflictAction, RunOutcome, UploadLimits
from app.domain.ratelimit import TokenBucket
from app.domain.retry import RetryPolicy
from app.infrastructure.db import open_database
from app.infrastructure.repositories import Repos
from tests.conftest import Env, never_duplicate


def resolver(action: ConflictAction, apply_all=False):
    calls = []

    async def r(info):
        calls.append(info)
        return ConflictDecision(action, apply_all)

    r.calls = calls  # type: ignore[attr-defined]
    return r


async def fail_resolver(info):
    raise AssertionError(f"unexpected conflict prompt: {info.rel_path}")


def restorer(env: Env) -> RestoreService:
    async def nosleep(d):
        pass

    return RestoreService(env.repos, env.gateway, TokenBucket(None), RetryPolicy(base_delay=0.001, jitter=0, max_attempts=2), nosleep)


async def backed_up_env(tmp_path, files: dict[str, bytes], limits=None):
    env = Env(tmp_path, limits=limits)
    for rel, data in files.items():
        env.write(rel, data)
    sid = await env.new_storage()
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.COMPLETED
    return env, sid


async def test_restore_roundtrip_structure_hash_and_mtime(tmp_path):
    files = {"a.txt": b"hello" * 500, "Sub/Deep/b.bin": os.urandom(9000), "Sub/c.bin": b""}
    env, sid = await backed_up_env(tmp_path, files)
    dest = tmp_path / "restored"
    s = await restorer(env).restore(restorer(env).file_ids_for(sid), str(dest), fail_resolver)
    assert s.outcome == RunOutcome.COMPLETED and s.succeeded == 3
    for rel, data in files.items():
        assert (dest / "Photography" / rel).read_bytes() == data
    assert not list(dest.rglob("*.teloude-part"))
    assert (dest / "Photography" / "a.txt").stat().st_mtime_ns == (env.src / "a.txt").stat().st_mtime_ns


async def test_restore_multi_volume_file(tmp_path):
    data = os.urandom(8 * 1024 * 3 + 100)
    env, sid = await backed_up_env(tmp_path, {"big.bin": data}, UploadLimits(part_size=1024, max_parts=8))
    dest = tmp_path / "out"
    s = await restorer(env).restore(restorer(env).file_ids_for(sid), str(dest), fail_resolver)
    assert s.succeeded == 1 and (dest / "Photography" / "big.bin").read_bytes() == data


async def test_existing_destination_requires_explicit_decision(tmp_path):
    env, sid = await backed_up_env(tmp_path, {"a.txt": b"cloud version"})
    dest = tmp_path / "out"
    target = dest / "Photography" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"precious local data")
    ids = restorer(env).file_ids_for(sid)

    skip = resolver(ConflictAction.SKIP)
    s = await restorer(env).restore(ids, str(dest), skip)
    assert len(skip.calls) == 1 and target.read_bytes() == b"precious local data"  # NOT silently overwritten
    assert s.skipped == 1 and s.succeeded == 0

    s = await restorer(env).restore(ids, str(dest), resolver(ConflictAction.KEEP_BOTH))
    assert target.read_bytes() == b"precious local data"
    assert (dest / "Photography" / "a (1).txt").read_bytes() == b"cloud version" and s.succeeded == 1

    s = await restorer(env).restore(ids, str(dest), resolver(ConflictAction.CANCEL))
    assert s.outcome == RunOutcome.CANCELLED and target.read_bytes() == b"precious local data"

    s = await restorer(env).restore(ids, str(dest), resolver(ConflictAction.REPLACE))
    assert target.read_bytes() == b"cloud version" and s.succeeded == 1


async def test_path_traversal_in_cloud_record_is_blocked(tmp_path):
    env, sid = await backed_up_env(tmp_path, {"a.txt": b"x" * 10, "b.txt": b"y" * 10})
    env.db.execute("UPDATE files SET rel_path='Photography/../../escape.txt' WHERE name='a.txt'")
    env.db.execute("UPDATE files SET rel_path='C:\\Windows\\evil.txt' WHERE name='b.txt'")
    dest = tmp_path / "jail" / "out"
    s = await restorer(env).restore(restorer(env).file_ids_for(sid), str(dest), fail_resolver)
    assert s.outcome == RunOutcome.FAILED and s.failed == 2 and s.succeeded == 0
    assert not (tmp_path / "jail" / "escape.txt").exists() and not (tmp_path / "escape.txt").exists()
    assert not list((tmp_path).rglob("evil.txt"))


async def test_corrupted_download_is_rejected_and_never_placed(tmp_path):
    env, sid = await backed_up_env(tmp_path, {"a.bin": os.urandom(5000)})
    for m in env.gateway.all_messages():
        m.data = m.data[:-1] + bytes([m.data[-1] ^ 0xFF])  # bit-rot in the cloud
    dest = tmp_path / "out"
    s = await restorer(env).restore(restorer(env).file_ids_for(sid), str(dest), fail_resolver)
    assert s.outcome == RunOutcome.FAILED and "checksum" in s.errors[0]
    assert not (dest / "Photography" / "a.bin").exists()  # a corrupt file is never presented as restored


async def test_restore_retries_transient_errors_and_resumes_download(tmp_path):
    data = os.urandom(CHUNK * 3 + 77)
    env, sid = await backed_up_env(tmp_path, {"a.bin": data})
    state = {"n": 0}

    def hook(op, n):
        if op == "download":
            state["n"] += 1
            if state["n"] == 2:
                return TransientNetworkError("blip")

    env.gateway.fail_hook = hook
    dest = tmp_path / "out"
    s = await restorer(env).restore(restorer(env).file_ids_for(sid), str(dest), fail_resolver)
    assert s.outcome == RunOutcome.COMPLETED and (dest / "Photography" / "a.bin").read_bytes() == data
    assert env.gateway.reconnects == 1


async def test_restore_resumes_from_partial_temp_file(tmp_path):
    data = os.urandom(CHUNK * 4)
    env, sid = await backed_up_env(tmp_path, {"a.bin": data})
    dest = tmp_path / "out"
    (dest / "Photography").mkdir(parents=True)
    (dest / "Photography" / "a.bin.teloude-part").write_bytes(data[: CHUNK * 2 + 5])  # leftover of an interrupted restore
    s = await restorer(env).restore(restorer(env).file_ids_for(sid), str(dest), fail_resolver)
    assert s.outcome == RunOutcome.COMPLETED and (dest / "Photography" / "a.bin").read_bytes() == data
    assert env.gateway.download_chunk_calls == 2  # only the missing chunks were fetched


async def test_restore_cancel_is_not_success_and_keeps_partial(tmp_path):
    env, sid = await backed_up_env(tmp_path, {f"f{i}.bin": os.urandom(2000) for i in range(3)})
    control = TransferControl()
    from app.application.restore import RestoreEvents

    ev = RestoreEvents(on_file_start=lambda *a: control.cancel() if a[3] == 1 else None)
    s = await restorer(env).restore(restorer(env).file_ids_for(sid), str(tmp_path / "o"), fail_resolver, control, ev)
    assert s.outcome == RunOutcome.CANCELLED and s.succeeded == 1 and s.cancelled == 2


async def test_another_pc_discovers_storage_and_restores_without_original_db(tmp_path):
    files = {"Portraits/2026/p.jpg": os.urandom(4000), "top.txt": b"t" * 300}
    env, sid = await backed_up_env(tmp_path, files)
    # --- a *different* PC: brand-new empty database, same Telegram account
    db2 = open_database(tmp_path / "pc2" / "t.db")
    repos2 = Repos.create(db2)
    svc2 = StorageService(repos2, env.gateway)
    new = await svc2.discover()
    assert len(new) == 1
    rep = await svc2.sync_storage(new[0])
    assert rep.files_found == 2 and rep.incomplete == 0
    folder = repos2.folders.by_path(new[0], "Photography/Portraits/2026")
    assert folder is not None and folder.tg_topic_id is not None
    rs = RestoreService(repos2, env.gateway, TokenBucket(None))
    dest = tmp_path / "pc2_out"
    s = await rs.restore(rs.file_ids_for(new[0]), str(dest), fail_resolver)
    assert s.outcome == RunOutcome.COMPLETED
    for rel, data in files.items():
        assert (dest / "Photography" / rel).read_bytes() == data
    assert await svc2.discover() == []  # idempotent


async def test_multiple_independent_storages(env: Env):
    a, b = await env.new_storage("Photography"), await env.new_storage("Projects")
    env.write("x.txt", b"x" * 100)
    await env.backup.run(a, str(env.src), never_duplicate)
    assert env.repos.storages.usage(a) == (1, 100) and env.repos.storages.usage(b) == (0, 0)
    assert {s["title"] for s in env.repos.storages.list()} == {"Teloude - Photography", "Teloude - Projects"}


async def test_cloud_deletion_requires_confirmation_and_never_touches_local(env: Env):
    p = env.write("x.txt", b"x" * 100)
    sid = await env.new_storage()
    await env.backup.run(sid, str(env.src), never_duplicate)
    fid = env.repos.files.list_backed_up(sid)[0]["id"]
    with pytest.raises(PermissionError):
        await env.storage.delete_cloud_files([fid], confirmed=False)
    assert len(env.gateway.all_messages()) == 1
    assert await env.storage.delete_cloud_files([fid], confirmed=True) == 1
    assert env.gateway.all_messages() == [] and p.exists()
    assert env.repos.files.list_backed_up(sid) == []


async def test_search_and_preview(tmp_path):
    from PIL import Image

    env = Env(tmp_path)
    img = env.src
    Image.new("RGB", (64, 48), (200, 30, 30)).save(img / "red photo.png")
    (img / "notes.txt").write_text("hello preview")
    sid = await env.new_storage()
    await env.backup.run(sid, str(env.src), never_duplicate)
    hits = SearchService(env.repos).search("RED", type_group="Images")
    assert [h.name for h in hits] == ["red photo.png"] and hits[0].storage_title == "Teloude - Photography"
    assert [h.name for h in SearchService(env.repos).search("photography")] == ["notes.txt", "red photo.png"]  # storage/folder match
    prev = PreviewService(env.repos, env.gateway, tmp_path / "cache")
    r = await prev.get(hits[0].file_id)
    assert r.kind == "image" and r.png and r.png.startswith(b"\x89PNG")
    # no local copy -> previewed from a cloud download into the cache; original is untouched
    env.db.execute("UPDATE files SET local_path=NULL")
    txt = env.repos.files.search("notes")[0]["id"]
    r = await prev.get(txt)
    assert r.kind == "text" and r.text == "hello preview"
    assert [m.data for m in env.gateway.all_messages() if m.filename == "notes.txt"] == [b"hello preview"]
    env.db.execute("UPDATE files SET ext='xyz' WHERE id=?", (txt,))
    assert (await prev.get(txt)).kind == "none"  # graceful fallback
