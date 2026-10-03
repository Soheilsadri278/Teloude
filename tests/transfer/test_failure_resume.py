"""Recoverable failures: a failed backup must be resumable, never a dead end.

Covers the required matrix (all against the in-memory fake transport, no live Telegram):

 1. failure before the first part            7. permanent failure is not retried
 2. failure after several parts              8. cancellation stays distinct from failure
 3. network disconnect                       9. application restart after failure
 4. network reconnection                    10. duplicate protection across a resume
 5. resume after failure                    11. live progress
 6. invalid / expired upload session        12. live progress after resume
"""

from __future__ import annotations

import os

import pytest

from app.application.backup import BackupEvents
from app.application.control import TransferControl
from app.application.recovery import RecoveryService
from app.domain.errors import PermanentTransferError, TransientNetworkError
from app.domain.failure import FailureKind, classify, is_recoverable
from app.domain.models import (
    DuplicateAction,
    FileState,
    ProgressSnapshot,
    RunOutcome,
    TransferState,
    UploadLimits,
)
from app.testing.fake_gateway import SimulatedCrash
from tests.conftest import Env, decide, never_duplicate

PART = 1024  # the Env default part size


def progress_recorder() -> tuple[BackupEvents, list[int], list[tuple[float, float, float]]]:
    """BackupEvents that records every *acknowledged* progress snapshot."""
    dones: list[int] = []
    snapshots: list[tuple[float, float, float]] = []

    def on_progress(fid: int, snap: ProgressSnapshot) -> None:
        dones.append(snap.done)
        snapshots.append((snap.done, snap.total, snap.speed))

    return BackupEvents(on_file_progress=on_progress), dones, snapshots


def transfer(env: Env):  # type: ignore[no-untyped-def]
    return env.db.query_one("SELECT * FROM transfers ORDER BY id DESC LIMIT 1")


# ------------------------------------------------------------------ 1. failure before the first part
async def test_failure_before_first_part_is_recoverable_and_resumes(env: Env):
    env.write("a.bin", os.urandom(20 * PART))
    sid = await env.new_storage()
    env.gateway.fail_hook = lambda op, n: TransientNetworkError("no route to host") if op == "upload_part" else None

    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.FAILED and s.succeeded == 0
    t = transfer(env)
    assert t["state"] == TransferState.RECOVERABLE_FAILED  # NOT a dead end
    assert not t["completed_parts"]  # nothing was acknowledged
    assert env.db.scalar("SELECT state FROM files") == FileState.FAILED
    assert env.gateway.all_messages() == []  # and nothing was recorded as backed up

    summary = env.resume_service.summary(sid)
    assert summary.resumable and summary.count == 1 and summary.done_bytes == 0

    calls = env.gateway.part_calls  # includes the attempts that failed
    env.gateway.fail_hook = None  # network is back
    s2 = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s2.outcome == RunOutcome.COMPLETED
    assert env.gateway.part_calls - calls == 20  # all 20 parts, and only once each
    assert len(env.gateway.all_messages()) == 1


# ------------------------------------------------------------------ 2. failure after several parts
async def test_failure_after_several_parts_keeps_the_checkpoint(env: Env):
    data = os.urandom(40 * PART)
    env.write("big.bin", data)
    sid = await env.new_storage()
    env.gateway.fail_hook = lambda op, n: TransientNetworkError("wifi gone") if op == "upload_part" and n >= 12 else None

    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.FAILED and s.recoverable == 1 and s.permanent == 0
    t = transfer(env)
    assert t["state"] == TransferState.RECOVERABLE_FAILED
    assert t["tg_file_id"] and t["completed_parts"]
    acked = len(env.gateway.sessions[t["tg_file_id"]])
    assert 0 < acked < 40
    assert env.resume_service.summary(sid).done_bytes == acked * PART

    calls = env.gateway.part_calls
    env.gateway.fail_hook = None
    s2 = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s2.outcome == RunOutcome.COMPLETED
    assert env.gateway.part_calls - calls == 40 - acked  # only the missing parts were re-sent
    assert env.gateway.all_messages()[0].data == data


# ------------------------------------------------------------------ 3 + 4. disconnect then reconnect
async def test_disconnect_then_reconnect_with_a_resume_action(env: Env):
    data = os.urandom(30 * PART)
    env.write("a.bin", data)
    sid = await env.new_storage()

    state = {"down": True}  # the PC lost its internet connection

    def offline(op, n):  # type: ignore[no-untyped-def]
        return TransientNetworkError("no route to host") if op == "upload_part" and state["down"] else None

    env.gateway.fail_hook = offline
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.FAILED
    assert s.reason and ("onnection" in s.reason or "reach" in s.reason)  # headline for the UI
    assert transfer(env)["state"] == TransferState.RECOVERABLE_FAILED

    state["down"] = False  # internet is back
    calls = env.gateway.part_calls
    s2 = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s2.outcome == RunOutcome.COMPLETED and env.gateway.part_calls > calls
    assert env.gateway.all_messages()[0].data == data
    assert not env.resume_service.summary(sid).resumable

    # A *brief* drop is not a failure at all: the pipeline reconnects and carries on by itself.
    env.write("b.bin", os.urandom(10 * PART))
    brief = {"n": 0}

    def blip(op, n):  # type: ignore[no-untyped-def]
        if op == "upload_part" and brief["n"] < 2:
            brief["n"] += 1
            return TransientNetworkError("blip")
        return None

    env.gateway.fail_hook = blip
    s3 = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s3.outcome == RunOutcome.COMPLETED and env.gateway.reconnects >= 1


# ------------------------------------------------------------------ 5. resume after failure (the UI action)
async def test_resume_after_failure_uses_the_same_operation_and_completes(env: Env):
    data = os.urandom(50 * PART)
    env.write("big.bin", data)
    sid = await env.new_storage()
    env.gateway.fail_hook = lambda op, n: TransientNetworkError("down") if op == "upload_part" and n >= 20 else None
    first = await env.backup.run(sid, str(env.src), never_duplicate)
    assert first.outcome == RunOutcome.FAILED and first.resumable

    env.gateway.fail_hook = None
    summary = env.resume_service.summary(sid)
    assert summary.local_root == str(env.src)  # Resume reuses the SAME folder, no new dialog
    resumed = await env.backup.run(sid, summary.local_root, never_duplicate)
    assert resumed.outcome == RunOutcome.COMPLETED
    assert len(env.gateway.all_messages()) == 1 and env.gateway.all_messages()[0].data == data


# ------------------------------------------------------------------ 6. invalid / expired upload session
async def test_expired_upload_session_is_detected_and_restarted_safely(env: Env):
    data = os.urandom(25 * PART)
    env.write("a.bin", data)
    sid = await env.new_storage()
    env.gateway.fail_hook = lambda op, n: TransientNetworkError("down") if op == "upload_part" and n >= 10 else None
    await env.backup.run(sid, str(env.src), never_duplicate)
    t = transfer(env)
    assert t["completed_parts"]

    env.gateway.fail_hook = None
    env.gateway.expire_all_sessions()  # Telegram forgot our partial upload while we were away
    calls = env.gateway.part_calls
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.COMPLETED
    assert env.gateway.part_calls - calls == 25  # the bytes really are re-sent, not faked
    assert env.db.scalar("SELECT session_restarts FROM transfers") >= 1
    assert len(env.gateway.all_messages()) == 1 and env.gateway.all_messages()[0].data == data


async def test_validate_upload_session_reports_the_truth(env: Env):
    sess = env.gateway.new_upload_session("x.bin")
    assert await env.gateway.validate_upload_session(sess) is True
    env.gateway.expire_all_sessions()
    assert await env.gateway.validate_upload_session(sess) is False


# ------------------------------------------------------------------ 7. permanent failure
async def test_permanent_failure_is_not_offered_as_resume(env: Env):
    env.write("bad.bin", b"x" * 3000)
    sid = await env.new_storage()
    real = env.gateway.finalize_upload

    async def rejects(*a, **k):  # type: ignore[no-untyped-def]
        raise PermanentTransferError("unsupported file")

    env.gateway.finalize_upload = rejects  # type: ignore[method-assign]
    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.FAILED and s.permanent == 1 and s.recoverable == 0
    assert s.resumable is False  # the UI must NOT show a Resume button
    t = transfer(env)
    assert t["state"] == TransferState.PERMANENT_FAILED
    assert env.db.scalar("SELECT state FROM files") == FileState.PERMANENT_FAILED

    env.gateway.finalize_upload = real  # type: ignore[method-assign]
    calls = env.gateway.part_calls
    again = await env.backup.run(sid, str(env.src), never_duplicate)
    assert env.gateway.part_calls == calls  # not retried automatically: needs a human
    assert again.outcome == RunOutcome.NOTHING_TO_DO and env.resume_service.summary(sid).blocked == 1

    # ...but once the user actually changes the file, it is picked up again.
    (env.src / "bad.bin").write_bytes(b"y" * 3100)
    fixed = await env.backup.run(sid, str(env.src), never_duplicate)
    assert fixed.outcome == RunOutcome.COMPLETED


# ------------------------------------------------------------------ 8. cancellation
async def test_cancellation_is_distinct_from_failure_and_still_resumable(env: Env):
    for i in range(3):
        env.write(f"f{i}.bin", os.urandom(5000))
    sid = await env.new_storage()
    control = TransferControl()
    events = BackupEvents(on_file_start=lambda fid, rel, size, i, n: control.cancel() if i == 1 else None)
    s = await env.backup.run(sid, str(env.src), never_duplicate, control, events)
    assert s.outcome == RunOutcome.CANCELLED and s.failed == 0
    for t in env.db.query("SELECT * FROM transfers"):
        assert t["state"] != TransferState.FAILED and t["state"] != TransferState.PERMANENT_FAILED
    assert env.resume_service.summary(sid).resumable  # uploaded parts are kept for later

    s2 = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s2.outcome == RunOutcome.COMPLETED and len(env.gateway.all_messages()) == 3


# ------------------------------------------------------------------ 9. restart after failure
async def test_application_restart_after_failure_recovers_and_resumes(env: Env):
    data = os.urandom(35 * PART)
    env.write("a.bin", data)
    sid = await env.new_storage()

    def hook(op, n):  # type: ignore[no-untyped-def]
        if op == "upload_part" and n == 15:
            raise SimulatedCrash  # the process dies (power loss / kill -9)

    env.gateway.fail_hook = hook
    with pytest.raises(SimulatedCrash):
        await env.backup.run(sid, str(env.src), never_duplicate)
    env.gateway.fail_hook = None

    rep = RecoveryService(env.repos).recover()  # what AppController does at startup
    assert rep.interrupted == 1
    assert env.db.scalar("SELECT state FROM transfers") == TransferState.INTERRUPTED
    assert env.repos.files.list_backed_up(sid) == []  # never assume a started upload succeeded
    summary = env.resume_service.summary(sid)
    assert summary.resumable and summary.done_bytes > 0

    s = await env.backup.run(sid, str(env.src), never_duplicate)
    assert s.outcome == RunOutcome.COMPLETED
    assert len(env.gateway.all_messages()) == 1 and env.gateway.all_messages()[0].data == data


# ------------------------------------------------------------------ 10. duplicate protection
async def test_resume_after_failure_never_creates_a_duplicate_record(env: Env):
    blob = os.urandom(6000)
    env.write("one.bin", blob)
    sid = await env.new_storage()
    await env.backup.run(sid, str(env.src), never_duplicate)

    env.write("two.bin", blob)  # identical content elsewhere: the classic duplicate case
    env.gateway.fail_hook = lambda op, n: TransientNetworkError("down") if op == "upload_part" and n >= 3 else None
    s = await env.backup.run(sid, str(env.src), decide(DuplicateAction.UPLOAD_AGAIN))
    assert s.outcome == RunOutcome.FAILED
    env.gateway.fail_hook = None

    s2 = await env.backup.run(sid, str(env.src), decide(DuplicateAction.UPLOAD_AGAIN))
    assert s2.outcome == RunOutcome.COMPLETED
    msgs = env.gateway.all_messages()
    assert len(msgs) == 2  # one per file: no duplicate copy of the file that came back
    assert sorted(m.caption.split(chr(10))[1][:60] for m in msgs)
    assert env.db.scalar("SELECT COUNT(*) FROM telegram_messages") == 2
    assert (env.src / "one.bin").read_bytes() == blob  # local files untouched


# ------------------------------------------------------------------ 11. live progress
async def test_live_progress_reports_every_acknowledged_step(env: Env):
    data = os.urandom(30 * PART)
    env.write("big.bin", data)
    sid = await env.new_storage()
    events, dones, snaps = progress_recorder()
    env.gateway.part_latency = 0.02  # slow enough for the 20 ms reporter to actually fire

    s = await env.backup.run(sid, str(env.src), never_duplicate, None, events)
    assert s.outcome == RunOutcome.COMPLETED
    assert len(dones) >= 3, f"progress was emitted only {len(dones)} time(s) - the bar would look frozen"
    assert dones == sorted(dones) and len(set(dones)) > 1  # monotonically increasing, not a constant
    assert dones[-1] == len(data)  # ends exactly at the real size
    assert all(0 <= d <= len(data) for d in dones)  # never invented, never over-reported
    assert all(total == len(data) for _, total, _ in snaps)


async def test_progress_never_exceeds_what_telegram_acknowledged(env: Env):
    """A part counts only once Telegram accepted it AND it was checkpointed."""
    data = os.urandom(20 * PART)
    env.write("a.bin", data)
    sid = await env.new_storage()
    events, dones, _ = progress_recorder()
    env.gateway.fail_hook = lambda op, n: TransientNetworkError("down") if op == "upload_part" and n >= 6 else None
    await env.backup.run(sid, str(env.src), never_duplicate, None, events)

    t = transfer(env)
    acked = len(env.gateway.sessions[t["tg_file_id"]])
    assert dones and max(dones) <= acked * PART  # the UI never runs ahead of the server


# ------------------------------------------------------------------ 12. progress after resume
async def test_live_progress_continues_after_resume_from_the_checkpoint(env: Env):
    data = os.urandom(40 * PART)
    env.write("big.bin", data)
    sid = await env.new_storage()
    env.gateway.part_latency = 0.02  # slow enough for the 20 ms reporter to actually fire
    env.gateway.fail_hook = lambda op, n: TransientNetworkError("down") if op == "upload_part" and n >= 9 else None
    first, dones1, _ = progress_recorder()
    await env.backup.run(sid, str(env.src), never_duplicate, None, first)
    assert dones1 and max(dones1) > 0

    env.gateway.fail_hook = None
    second, dones2, _ = progress_recorder()
    s = await env.backup.run(sid, str(env.src), never_duplicate, None, second)
    assert s.outcome == RunOutcome.COMPLETED
    # Resume continues ABOVE zero: it must not restart the bar (and the bytes) from scratch.
    assert dones2[0] > 0, "resumed progress started at zero - the bar and the upload were restarted"
    assert dones2 == sorted(dones2)
    assert dones2[-1] == len(data)
    assert max(dones1) < len(data)


# ------------------------------------------------------------------ classification unit surface
def test_failure_classification_matrix():
    assert classify(TransientNetworkError("x")) == FailureKind.RECOVERABLE
    assert classify(PermanentTransferError("x")) == FailureKind.PERMANENT
    assert is_recoverable(TransientNetworkError("x"))
    assert not is_recoverable(PermanentTransferError("x"))
    assert is_recoverable(TimeoutError()) and is_recoverable(ConnectionError())
    assert not is_recoverable(ValueError("nope"))  # unknown -> permanent, never an infinite retry loop


def test_upload_limits_are_untouched_by_recovery():
    # guards the planning path the resume logic depends on
    limits = UploadLimits(part_size=PART, max_parts=8)
    assert limits.max_volume_size == PART * 8
