from __future__ import annotations

import os

import pytest

from app.application.backup import BackupEvents
from app.application.control import TransferControl
from app.application.metadata import decode_caption
from app.application.recovery import RecoveryService
from app.domain.errors import TransientNetworkError
from app.domain.models import DuplicateAction, FileState, RunOutcome, TransferState, UploadLimits
from app.testing.fake_gateway import SimulatedCrash
from tests.conftest import Env, decide, never_duplicate


def tree(root) -> dict[str, bytes]:
    """Local files, keyed by their path relative to ``root`` with '/' separators.

    ``as_posix()`` (not ``str()``) because these keys are compared with the logical paths Teloude
    stores and uploads, which are always '/'-separated - on Windows ``str(Path)`` would produce
    backslashes and silently stop matching.
    """
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


async def test_tree_helper_keys_are_always_posix(env: Env):
    """``tree()`` keys are compared with logical paths, which are always '/'-separated.

    ``str(Path(...))`` would produce backslashes on Windows and silently stop matching, so this
    pins the helper to ``as_posix()`` (and to never containing a backslash) on every platform.
    """
    env.write("a/b/c.txt", b"x")
    env.write("top.txt", b"y")
    keys = set(tree(env.src))
    assert keys == {"a/b/c.txt", "top.txt"}
    assert not any("\\" in k for k in keys)


async def test_backup_end_to_end_hierarchy_mapping_and_content(env: Env):
    env.write("a.txt", b"root file")
    env.write("Portraits/2026/p1.jpg", os.urandom(5000))
    env.write("Portraits/2025/p0.jpg", os.urandom(3000))
    env.write("Studio/s.raw", b"S" * 10)
    sid = await env.new_storage()
    before = tree(env.src)
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.COMPLETED and s.succeeded == 4 and s.failed == 0
    chat = next(iter(env.gateway.chats.values()))
    assert sorted(chat.topics.values()) == [
        "Photography", "Photography / Portraits / 2025", "Photography / Portraits / 2026", "Photography / Studio",
    ]  # fmt: skip
    # DB maps local folder -> logical folder -> telegram topic
    f = env.repos.folders.by_path(sid, "Photography/Portraits/2026")
    assert chat.topics[f.tg_topic_id] == "Photography / Portraits / 2026"
    uploaded = {decode_caption(m.caption).rel_path: m.data for m in env.gateway.all_messages()}
    assert uploaded["Photography/Portraits/2026/p1.jpg"] == before["Portraits/2026/p1.jpg"]
    assert all(r["state"] == FileState.BACKED_UP for r in env.repos.files.list_backed_up(sid))
    assert tree(env.src) == before  # local files untouched


async def test_logical_paths_in_the_index_captions_and_topics_are_posix(env: Env):
    """A Windows path separator must never leak into the DB, a caption or a topic title.

    Logical paths are the identity of a backed-up file: if they contained '\\' on Windows and '/'
    on Linux, the same storage restored on the other platform would not recognise its own files.
    """
    env.write("Portraits/2026/p1.jpg", b"x" * 100)
    env.write("Portraits/2025/p0.jpg", b"y" * 100)
    sid = await env.new_storage()
    await env.backup.run(sid, str(env.src), never_duplicate)

    rel_paths = sorted(r["rel_path"] for r in env.repos.files.list_backed_up(sid))
    assert rel_paths == ["Photography/Portraits/2025/p0.jpg", "Photography/Portraits/2026/p1.jpg"]
    for m in env.gateway.all_messages():
        assert "\\" not in decode_caption(m.caption).rel_path
    chat = next(iter(env.gateway.chats.values()))
    assert all("\\" not in title for title in chat.topics.values())


async def test_second_run_is_a_noop_and_touch_only_does_not_reupload(env: Env):
    p = env.write("a.bin", b"x" * 3000)
    sid = await env.new_storage()
    await env.backup.run(sid, str(env.src), never_duplicate)
    calls = env.gateway.part_calls
    again = await env.backup.run(sid, str(env.src), never_duplicate)
    assert again.outcome == RunOutcome.NOTHING_TO_DO and env.gateway.part_calls == calls
    os.utime(p, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))  # touched, same bytes
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert env.gateway.part_calls == calls and s.unchanged == 1
    assert len(env.gateway.all_messages()) == 1


async def test_changed_file_becomes_new_version_old_cloud_copy_kept(env: Env):
    p = env.write("a.txt", b"v1" * 600)
    sid = await env.new_storage()
    await env.backup.run(sid, str(env.src), never_duplicate)
    p.write_bytes(b"v2 is different" * 100)
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.succeeded == 1
    rows = env.db.query("SELECT version,state,is_current FROM files ORDER BY version")
    assert [(r["version"], r["state"], r["is_current"]) for r in rows] == [(1, "superseded", 0), (2, "backed_up", 1)]
    assert len(env.gateway.all_messages()) == 2  # nothing deleted from Telegram either


async def test_large_file_is_split_into_volumes_and_pipelined(tmp_path):
    env = Env(tmp_path, limits=UploadLimits(part_size=1024, max_parts=8))  # 8 KiB volumes
    data = os.urandom(8 * 1024 * 2 + 3000)
    env.write("big.bin", data)
    sid = await env.new_storage()
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.COMPLETED
    msgs = sorted(env.gateway.all_messages(), key=lambda m: decode_caption(m.caption).volume_index)
    assert len(msgs) == 3 and b"".join(m.data for m in msgs) == data
    assert msgs[0].filename.endswith("001of003")
    row = env.repos.files.list_backed_up(sid)[0]
    assert row["volumes_total"] == 3 and len(env.repos.messages.for_file(row["id"])) == 3
    assert env.gateway.max_inflight >= 1


async def test_file_is_not_marked_backed_up_when_upload_fails(env: Env):
    env.write("a.bin", b"z" * 4000)
    sid = await env.new_storage()
    env.gateway.fail_hook = lambda op, n: TransientNetworkError("down") if op == "upload_part" else None
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.FAILED and s.succeeded == 0
    row = env.db.query_one("SELECT * FROM files")
    assert row["state"] == FileState.FAILED and row["last_error"]
    assert env.repos.files.list_backed_up(sid) == [] and env.gateway.all_messages() == []


async def test_partial_outcome_when_one_file_fails(env: Env):
    env.write("good1.txt", b"1" * 100)
    env.write("good2.txt", b"2" * 100)
    env.write("bad.txt", b"3" * 100)
    sid = await env.new_storage()
    real = env.gateway.finalize_upload

    async def flaky(storage, topic, session, parts, filename, caption):
        if filename == "bad.txt":
            from app.domain.errors import PermanentTransferError

            raise PermanentTransferError("rejected")
        return await real(storage, topic, session, parts, filename, caption)

    env.gateway.finalize_upload = flaky  # type: ignore[method-assign]
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.PARTIAL and (s.succeeded, s.failed) == (2, 1)


async def test_cancel_is_not_success_and_checkpoint_allows_resume(env: Env):
    for i in range(4):
        env.write(f"f{i}.bin", os.urandom(4000))
    sid = await env.new_storage()
    control = TransferControl()
    events = BackupEvents(on_file_start=lambda fid, rel, size, i, n: control.cancel() if i == 2 else None)
    s = await env.backup.run(sid, str(env.src), never_duplicate, control, events)
    assert s.outcome == RunOutcome.CANCELLED and s.succeeded == 2 and s.cancelled == 2
    assert len(env.repos.files.list_backed_up(sid)) == 2
    s2 = await env.backup.run(sid, str(env.src), never_duplicate)  # next run finishes the rest only
    assert s2.outcome == RunOutcome.COMPLETED and s2.succeeded == 2 and len(env.gateway.all_messages()) == 4


async def test_pause_between_files_then_resume(env: Env):
    import asyncio

    for i in range(3):
        env.write(f"f{i}.bin", os.urandom(2000))
    sid = await env.new_storage()
    control = TransferControl()
    control.pause()
    task = asyncio.create_task(env.backup.run(sid, str(env.src), never_duplicate, control))
    await asyncio.sleep(0.2)
    assert not task.done() and env.gateway.all_messages() == []  # truly paused
    control.resume()
    s = await task
    assert s.outcome == RunOutcome.COMPLETED and s.succeeded == 3


# ------------------------------------------------------------------ duplicates
async def test_duplicate_prompt_skip_upload_again_cancel_and_apply_to_all(env: Env):
    same = os.urandom(2500)
    env.write("orig.bin", same)
    sid = await env.new_storage()
    await env.backup.run(sid, str(env.src), never_duplicate)
    env.write("copy1.bin", same)
    skip = decide(DuplicateAction.SKIP)
    s = await env.backup.run(sid, str(env.src), skip)
    assert len(skip.calls) == 1 and skip.calls[0].existing_paths == ["Photography/orig.bin"]
    assert s.skipped == 1 and len(env.gateway.all_messages()) == 1
    assert env.db.scalar("SELECT state FROM files WHERE name='copy1.bin'") == FileState.DUPLICATE_SKIPPED
    again = await env.backup.run(sid, str(env.src), never_duplicate)  # a skip is remembered: no re-prompt
    assert again.outcome == RunOutcome.NOTHING_TO_DO

    env.write("copy2.bin", same)
    env.write("copy3.bin", same)
    upl = decide(DuplicateAction.UPLOAD_AGAIN, apply_all=True)
    s = await env.backup.run(sid, str(env.src), upl)
    assert len(upl.calls) == 1  # "apply to all": asked once for two duplicates
    assert s.succeeded == 2 and len(env.gateway.all_messages()) == 3

    env.write("copy4.bin", same)
    s = await env.backup.run(sid, str(env.src), decide(DuplicateAction.CANCEL))
    assert s.outcome == RunOutcome.CANCELLED and len(env.gateway.all_messages()) == 3


# ------------------------------------------------------------------ resume / recovery / reconnect
async def test_network_outage_then_resume_sends_only_missing_parts(env: Env):
    data = os.urandom(40 * 1024)  # 40 parts
    env.write("big.bin", data)
    sid = await env.new_storage()
    state = {"down_after": 12}

    def hook(op, n):
        if op == "upload_part" and n > state["down_after"]:
            return TransientNetworkError("wifi gone")

    env.gateway.fail_hook = hook
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.FAILED and "unreachable" in " ".join(s.errors)
    t = env.db.query_one("SELECT * FROM transfers")
    # A temporary failure must NOT collapse into a dead end: it is recorded as recoverable and the
    # checkpoint (session id + acknowledged parts) is kept so Resume can continue from it.
    assert t["state"] == TransferState.RECOVERABLE_FAILED and t["completed_parts"] and t["tg_file_id"]
    assert s.recoverable == 1 and s.permanent == 0 and s.resumable
    assert env.db.scalar("SELECT state FROM files") == FileState.FAILED
    sent_before = len(env.gateway.sessions[t["tg_file_id"]])
    assert 0 < sent_before < 40
    summary = env.resume_service.summary(sid)
    assert summary.resumable and summary.done_bytes == sent_before * 1024
    env.gateway.fail_hook = None  # network is back
    calls = env.gateway.part_calls
    s2 = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s2.outcome == RunOutcome.COMPLETED
    assert env.gateway.part_calls - calls == 40 - sent_before  # NOT restarted from zero
    assert env.gateway.all_messages()[0].data == data
    assert not env.resume_service.summary(sid).resumable  # nothing left to resume


async def test_session_expired_at_finalize_restarts_with_new_session(env: Env):
    data = os.urandom(6000)
    env.write("a.bin", data)
    sid = await env.new_storage()
    hits = {"n": 0}

    def hook(op, n):
        if op == "finalize" and hits["n"] == 0:
            hits["n"] += 1
            env.gateway.expire_all_sessions()  # Telegram forgot our partial upload

    env.gateway.fail_hook = hook
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.COMPLETED and env.gateway.all_messages()[0].data == data
    assert env.db.scalar("SELECT session_restarts FROM transfers") == 1


async def test_crash_mid_upload_recovers_and_resumes_without_duplicates(env: Env):
    data = os.urandom(30 * 1024)
    env.write("a.bin", data)
    sid = await env.new_storage()

    def hook(op, n):
        if op == "upload_part" and n == 15:
            raise SimulatedCrash

    env.gateway.fail_hook = hook
    with pytest.raises(SimulatedCrash):
        await env.backup.run(sid, str(env.src), never_duplicate)
    env.gateway.fail_hook = None
    # --- "restart": recovery runs before anything else
    rep = RecoveryService(env.repos).recover()
    assert rep.interrupted >= 0 and env.db.scalar("SELECT state FROM files") in (FileState.PENDING, FileState.FAILED)
    assert env.repos.files.list_backed_up(sid) == []  # never assume a started upload succeeded
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.COMPLETED and len(env.gateway.all_messages()) == 1
    assert env.gateway.all_messages()[0].data == data


async def test_crash_after_telegram_accepted_message_adopts_it_instead_of_duplicating(env: Env):
    data = os.urandom(5000)
    env.write("a.bin", data)
    sid = await env.new_storage()
    env.gateway.crash_after_finalize = True  # message exists in Telegram, but we died before recording it
    with pytest.raises(SimulatedCrash):
        await env.backup.run(sid, str(env.src), never_duplicate)
    assert len(env.gateway.all_messages()) == 1 and env.repos.files.list_backed_up(sid) == []
    RecoveryService(env.repos).recover()
    calls = env.gateway.part_calls
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.COMPLETED
    assert len(env.gateway.all_messages()) == 1  # adopted, NOT uploaded twice
    assert env.gateway.part_calls == calls
    row = env.repos.files.list_backed_up(sid)[0]
    assert env.repos.messages.for_file(row["id"])[0]["message_id"] == env.gateway.all_messages()[0].id


async def test_recovery_discards_checkpoint_when_source_changed(env: Env):
    p = env.write("a.bin", os.urandom(20 * 1024))
    sid = await env.new_storage()

    def hook(op, n):
        if op == "upload_part" and n == 8:
            raise SimulatedCrash

    env.gateway.fail_hook = hook
    with pytest.raises(SimulatedCrash):
        await env.backup.run(sid, str(env.src), never_duplicate)
    env.gateway.fail_hook = None
    p.write_bytes(os.urandom(21 * 1024))  # edited while the app was dead
    rep = RecoveryService(env.repos).recover()
    assert rep.discarded_checkpoints == 1
    data = p.read_bytes()
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.COMPLETED and env.gateway.all_messages()[0].data == data


async def test_local_files_are_never_deleted_by_any_backup_path(env: Env):
    env.write("keep/1.txt", b"1" * 500)
    env.write("keep/2.txt", b"2" * 500)
    sid = await env.new_storage()
    before = tree(env.src)
    await env.backup.run(sid, str(env.src), never_duplicate)
    (env.src / "keep" / "2.txt").unlink()  # user deletes locally: must not cascade anywhere
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.missing_locally == 1 and len(env.gateway.all_messages()) == 2  # cloud copy untouched
    await env.backup.run(sid, str(env.src), decide(DuplicateAction.CANCEL))
    assert tree(env.src) == {k: v for k, v in before.items() if k != "keep/2.txt"}


async def test_unreadable_run_root_reports_failure_not_success(env: Env):
    sid = await env.new_storage()
    s = await env.backup.run(sid, str(env.tmp / "does-not-exist"), never_duplicate)
    assert s.outcome in (RunOutcome.FAILED, RunOutcome.PARTIAL, RunOutcome.NOTHING_TO_DO) and s.succeeded == 0
    assert s.errors  # the problem is surfaced
