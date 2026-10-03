from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.application.control import TransferControl
from app.application.pipeline import PipelineConfig, UploadPipeline
from app.domain.errors import (
    RateLimited,
    RetriesExhausted,
    TransferCancelled,
    TransientNetworkError,
    UploadSessionInvalid,
)
from app.domain.models import TransferState, UploadLimits
from app.domain.planning import RangeSet, plan_volumes
from app.domain.progress import ProgressTracker
from app.domain.ratelimit import TokenBucket
from app.domain.retry import RetryPolicy
from app.testing.fake_gateway import FakeGateway

PART = 1024


class MemSink:
    def __init__(self, delay: float = 0.0):
        self.saves: list[tuple[int, str, int]] = []
        self.delay = delay

    def save(self, tg_file_id, completed_parts, done_bytes):
        if self.delay:
            time.sleep(self.delay)  # deliberately BLOCKING: the pipeline must not care
        self.saves.append((tg_file_id, completed_parts, done_bytes))


def make_file(tmp: Path, parts: int, extra: int = 0) -> tuple[str, bytes]:
    data = bytes((i * 7 + 3) % 256 for i in range(parts * PART + extra))
    p = tmp / "f.bin"
    p.write_bytes(data)
    return str(p), data


def retry(max_attempts=3, base=0.5):
    return RetryPolicy(base_delay=base, max_delay=2.0, jitter=0.0, max_attempts=max_attempts)


async def run_volume(gw, path, data, *, cfg=None, limiter=None, control=None, session=None, completed=None, sink=None,
                     on_progress=None, on_state=None):  # fmt: skip
    limits = UploadLimits(part_size=PART, max_parts=4000)
    plan = plan_volumes(len(data), limits)[0]
    pipe = UploadPipeline(gw, limiter or TokenBucket(None), control or TransferControl(), cfg or PipelineConfig(), on_state=on_state)
    progress = ProgressTracker(len(data))
    completed = completed if completed is not None else RangeSet()
    sink = sink or MemSink()
    sess = await pipe.upload_volume(
        path=path, plan=plan, name="f.bin", session=session, completed=completed, checkpoint=sink,
        progress=progress, base_done=0, on_progress=on_progress,
    )  # fmt: skip
    return pipe, sess, progress, completed, sink


def assembled(gw: FakeGateway, sess) -> bytes:
    parts = gw.sessions[sess.file_id]
    return b"".join(parts[i] for i in sorted(parts))


# ------------------------------------------------------------------ pipelining
async def test_parts_are_pipelined_not_stop_and_wait(tmp_path):
    gw = FakeGateway(part_latency=0.05)
    path, data = make_file(tmp_path, 40)
    t0 = time.monotonic()
    pipe, sess, progress, *_ = await run_volume(gw, path, data, cfg=PipelineConfig(concurrency=4))
    elapsed = time.monotonic() - t0
    sequential = 40 * 0.05
    assert elapsed < sequential * 0.5, f"{elapsed:.2f}s is not meaningfully faster than sequential {sequential:.2f}s"
    assert gw.max_inflight > 1  # parts really overlapped
    assert assembled(gw, sess) == data  # ordered reconstruction is correct despite out-of-order sends
    assert progress.done == len(data)


async def test_concurrency_is_bounded(tmp_path):
    gw = FakeGateway(part_latency=0.01)
    path, data = make_file(tmp_path, 60)
    pipe, *_ = await run_volume(gw, path, data, cfg=PipelineConfig(concurrency=3))
    assert gw.max_inflight <= 3 and pipe.diag.max_inflight <= 3


async def test_odd_sized_tail_part_and_empty_file(tmp_path):
    gw = FakeGateway()
    path, data = make_file(tmp_path, 3, extra=17)
    _, sess, *_ = await run_volume(gw, path, data)
    assert assembled(gw, sess) == data
    Path(path).write_bytes(b"")
    _, sess, *_ = await run_volume(gw, path, b"")
    assert len(gw.sessions[sess.file_id]) == 1


# ------------------------------------------------------------------ retries / reconnect
async def test_transient_failures_reconnect_once_and_recover(tmp_path):
    failures = {"left": 6}

    def hook(op, n):
        if op == "upload_part" and failures["left"] > 0:
            failures["left"] -= 1
            return TransientNetworkError("boom")

    gw = FakeGateway(fail_hook=hook, part_latency=0.01)
    path, data = make_file(tmp_path, 12)
    sleeps: list[float] = []

    async def sleep(d):
        sleeps.append(d)
        await asyncio.sleep(0)  # a real sleep always yields to other workers

    pipe, sess, progress, *_ = await run_volume(gw, path, data, cfg=PipelineConfig(concurrency=4, retry=retry(5), sleep=sleep))
    assert assembled(gw, sess) == data
    assert pipe.diag.retries == 6
    # single-flight reconnect: 4 workers failing together must not trigger 4 separate reconnects
    assert 1 <= gw.reconnects < 6


async def test_retry_counter_does_not_accumulate_across_unrelated_failures(tmp_path):
    """Regression: attempt=1..N, success, next unrelated error must start again at the BASE delay."""
    failed_once: set[int] = set()
    seen_parts: list[int] = []

    def hook(op, n):
        # fail every part exactly once (20 separate, unrelated transient errors)
        if op == "upload_part" and n % 2 == 1:
            return TransientNetworkError("blip")

    gw = FakeGateway(fail_hook=hook)
    path, data = make_file(tmp_path, 20)
    sleeps: list[float] = []

    async def sleep(d):
        sleeps.append(d)
        await asyncio.sleep(0)  # a real sleep always yields to other workers

    cfg = PipelineConfig(concurrency=1, retry=retry(max_attempts=2, base=1.0), sleep=sleep)
    pipe, sess, *_ = await run_volume(gw, path, data, cfg=cfg)
    assert assembled(gw, sess) == data
    assert pipe.diag.retries >= 20 > cfg.retry.max_attempts * 2  # far more failures than any single budget
    # every delay is the base delay (1.0s, slept in <=0.25s chunks): nothing escalated to 2s/4s/30s
    assert max(sleeps) <= 0.25 + 1e-9
    assert sum(sleeps) == pytest.approx(pipe.diag.retries * 1.0, rel=0.01)
    assert failed_once == set() and seen_parts == []


async def test_retries_exhausted_keeps_checkpoint(tmp_path):
    def hook(op, n):
        if op == "upload_part" and n > 6:
            return TransientNetworkError("down")

    gw = FakeGateway(fail_hook=hook)
    path, data = make_file(tmp_path, 10)

    async def sleep(d):
        await asyncio.sleep(0)

    sink = MemSink()
    completed = RangeSet()
    with pytest.raises(RetriesExhausted):
        await run_volume(gw, path, data, cfg=PipelineConfig(concurrency=2, retry=retry(2), sleep=sleep), sink=sink, completed=completed)
    assert 0 < len(completed) < 10  # progress made before the outage is retained
    assert RangeSet.parse(sink.saves[-1][1]).serialize() == completed.serialize()


async def test_flood_wait_is_honoured_without_burning_attempts(tmp_path):
    state = {"n": 0}

    def hook(op, n):
        if op == "upload_part" and state["n"] < 3:
            state["n"] += 1
            return RateLimited(3)

    gw = FakeGateway(fail_hook=hook)
    path, data = make_file(tmp_path, 4)
    sleeps: list[float] = []

    async def sleep(d):
        sleeps.append(d)
        await asyncio.sleep(0)  # a real sleep always yields to other workers

    pipe, sess, *_ = await run_volume(gw, path, data, cfg=PipelineConfig(concurrency=1, retry=retry(1), sleep=sleep))
    assert assembled(gw, sess) == data
    assert pipe.diag.flood_waits == 3 and sum(sleeps) >= 3 * 3


async def test_invalid_session_starts_new_session_once(tmp_path):
    state = {"raised": False}

    def hook(op, n):
        if op == "upload_part" and n == 5 and not state["raised"]:
            state["raised"] = True
            return UploadSessionInvalid("FILE_ID_INVALID")

    gw = FakeGateway(fail_hook=hook)
    path, data = make_file(tmp_path, 10)
    first = gw.new_upload_session("f.bin")
    pipe, sess, progress, *_ = await run_volume(gw, path, data, session=first)
    assert sess.file_id != first.file_id and pipe.diag.session_restarts == 1
    assert assembled(gw, sess) == data and progress.done == len(data)


# ------------------------------------------------------------------ pause / cancel / resume
async def test_pause_stops_new_parts_lets_inflight_finish_then_resumes(tmp_path):
    gw = FakeGateway(part_latency=0.03)
    path, data = make_file(tmp_path, 40)
    control = TransferControl()
    states: list[TransferState] = []
    task = asyncio.create_task(run_volume(gw, path, data, cfg=PipelineConfig(concurrency=4), control=control, on_state=states.append))
    await asyncio.sleep(0.12)
    control.pause()
    await asyncio.sleep(0.15)  # let in-flight parts drain
    calls_when_paused = gw.part_calls
    assert gw.inflight == 0  # in-flight parts finished (not killed, not left hanging)
    await asyncio.sleep(0.2)
    assert gw.part_calls == calls_when_paused  # nothing new started while paused
    assert calls_when_paused < 40 and TransferState.PAUSED in states
    control.resume()
    pipe, sess, progress, *_ = await task
    assert assembled(gw, sess) == data and states[-1] == TransferState.UPLOADING


async def test_cancel_raises_and_checkpoint_survives(tmp_path):
    gw = FakeGateway(part_latency=0.02)
    path, data = make_file(tmp_path, 40)
    control = TransferControl()
    sink = MemSink()
    completed = RangeSet()
    task = asyncio.create_task(run_volume(gw, path, data, control=control, sink=sink, completed=completed))
    await asyncio.sleep(0.1)
    control.cancel()
    with pytest.raises(TransferCancelled):
        await task
    assert 0 < len(completed) < 40 and sink.saves  # NOT reported as success; progress persisted


async def test_resume_sends_only_missing_parts_not_restart_from_zero(tmp_path):
    gw = FakeGateway(part_latency=0.01)
    path, data = make_file(tmp_path, 30)
    control = TransferControl()
    completed = RangeSet()
    session = gw.new_upload_session("f.bin")
    # Cancel deterministically once a fixed number of parts were safely recorded, instead of racing
    # a wall-clock sleep (on a fast/loaded machine all 30 parts could finish before the cancel).
    def cancel_after_twelve(op: str, n: int) -> Exception | None:
        if op == "upload_part" and len(completed) >= 12:
            control.cancel()
        return None

    gw.fail_hook = cancel_after_twelve
    with pytest.raises(TransferCancelled):
        await run_volume(gw, path, data, control=control, session=session, completed=completed)
    gw.fail_hook = None
    done_before = len(completed)
    calls_before = gw.part_calls
    assert 0 < done_before < 30
    # "restart the app": same persisted session id + persisted completed set
    restored = RangeSet.parse(completed.serialize())
    pipe, sess, progress, *_ = await run_volume(gw, path, data, session=session, completed=restored)
    assert sess.file_id == session.file_id
    assert gw.part_calls - calls_before == 30 - done_before  # only the missing parts were sent
    assert assembled(gw, sess) == data and progress.done == len(data)


# ------------------------------------------------------------------ non-blocking observers
async def test_blocking_progress_callback_does_not_stall_upload(tmp_path):
    gw = FakeGateway(part_latency=0.01)
    path, data = make_file(tmp_path, 40)
    calls = []

    def slow_cb(snap):
        time.sleep(0.25)  # a UI that blocks for 250 ms on every update
        calls.append(snap.done)

    t0 = time.monotonic()
    await run_volume(gw, path, data, cfg=PipelineConfig(concurrency=4, progress_interval=0.02), on_progress=slow_cb)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.2, f"upload was held up by the progress callback ({elapsed:.2f}s)"
    assert calls[-1] == len(data)  # the final snapshot is still delivered


async def test_blocking_checkpoint_store_does_not_stall_upload(tmp_path):
    gw = FakeGateway(part_latency=0.01)
    path, data = make_file(tmp_path, 40)
    t0 = time.monotonic()
    pipe, *_ = await run_volume(gw, path, data, cfg=PipelineConfig(concurrency=4, checkpoint_interval=0.02), sink=MemSink(delay=0.2))
    assert time.monotonic() - t0 < 1.5
    assert pipe.diag.checkpoints_saved >= 1


# ------------------------------------------------------------------ speed limiter in the pipeline
async def test_speed_limit_applies_without_destroying_concurrency(tmp_path):
    # 512 KiB in 512 parts at 1 MiB/s -> ~0.5 s of throttling, no matter how fast the transport is.
    # The fake transport deliberately has no latency of its own here: on a platform whose event-loop
    # clock only ticks every ~15.6 ms (Windows) every simulated round trip costs a whole tick, so
    # 512 of them would add ~2 s of pure clock artefact and the test would measure the OS instead of
    # the limiter. Per-part behaviour with a coarse clock is pinned exactly - no wall clock involved
    # - by test_limiter_coalesces_micro_sleeps_on_a_coarse_clock.
    gw = FakeGateway()
    path, data = make_file(tmp_path, 512)  # 512 KiB
    limiter = TokenBucket(1024 * 1024, burst_seconds=0)  # 1 MiB/s -> ~0.5 s
    t0 = time.monotonic()
    pipe, *_ = await run_volume(gw, path, data, cfg=PipelineConfig(concurrency=4), limiter=limiter)
    elapsed = time.monotonic() - t0
    assert 0.4 < elapsed < 1.2
    assert gw.max_inflight > 1  # still pipelined while throttled
    assert pipe.diag.throttled_seconds > 0


async def test_unlimited_is_not_throttled(tmp_path):
    gw = FakeGateway()
    path, data = make_file(tmp_path, 200)
    pipe, *_ = await run_volume(gw, path, data, limiter=TokenBucket(None))
    assert pipe.diag.throttled_seconds == 0


async def test_progress_is_monotonic_and_reaches_total(tmp_path):
    gw = FakeGateway(part_latency=0.005)
    path, data = make_file(tmp_path, 30)
    seen: list[int] = []
    await run_volume(gw, path, data, cfg=PipelineConfig(progress_interval=0.01), on_progress=lambda s: seen.append(s.done))
    assert seen == sorted(seen) and seen[-1] == len(data)
