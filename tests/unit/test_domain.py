from __future__ import annotations

import asyncio
import math
import time

import pytest

from app.domain.errors import UnsafePathError
from app.domain.models import ProgressSnapshot, UploadLimits
from app.domain.paths import safe_join, sanitize_topic_title, split_relative, unique_sibling
from app.domain.planning import RangeSet, plan_volumes
from app.domain.progress import ProgressTracker
from app.domain.ratelimit import TokenBucket
from app.domain.retry import Backoff, RetryPolicy


# ---------------------------------------------------------------- rate limiter
async def test_unlimited_never_sleeps():
    calls = []

    async def sleep(d):
        calls.append(d)

    tb = TokenBucket(None, sleep=sleep)
    for _ in range(1000):
        assert await tb.acquire(10**9) == 0.0
    assert calls == []


async def test_limiter_enforces_average_rate_with_virtual_clock():
    t = [0.0]

    async def sleep(d):
        t[0] += d

    tb = TokenBucket(1000, burst_seconds=0, clock=lambda: t[0], sleep=sleep)
    for _ in range(10):
        await tb.acquire(500)
    assert t[0] == pytest.approx(5.0, rel=0.01)  # 5000 bytes at 1000 B/s


async def test_limiter_does_not_serialise_concurrent_workers_or_block_loop():
    """Regression: a blocking time.sleep in the limiter would starve every other coroutine."""
    tb = TokenBucket(2_000_000, burst_seconds=0)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    t0 = time.monotonic()
    await asyncio.gather(*(tb.acquire(100_000) for _ in range(8)))  # 800 KB @ 2 MB/s = 0.4 s total
    elapsed = time.monotonic() - t0
    hb.cancel()
    assert 0.3 < elapsed < 0.9
    assert ticks >= 15  # the event loop stayed responsive while throttled


async def test_limiter_coalesces_micro_sleeps_on_a_coarse_clock():
    """Regression: one sleep per part turned 0.5 s of throttling into 4 s.

    The pipeline asks the limiter for every 1 KiB part. On Windows the event loop's monotonic clock
    only advances in ~15.6 ms steps, so every one of those 512 sub-millisecond sleeps cost a whole
    tick: a 512 KiB / 1 MiB/s upload (0.5 s of budget) took 4.0 s. The limiter must bank short
    waits instead of sleeping for them. This runs on a virtual coarse clock, so it is exact and
    independent of the machine's real timer.
    """
    t = [0.0]
    tick = 15.625e-3  # the Windows default timer granularity
    sleeps = [0]

    async def sleep(d):
        sleeps[0] += 1
        t[0] += math.ceil(d / tick) * tick  # a coarse timer: only whole ticks

    tb = TokenBucket(1024 * 1024, burst_seconds=0, clock=lambda: t[0], sleep=sleep)
    for _ in range(512):  # 512 KiB, one 1 KiB part at a time, like the pipeline does
        await tb.acquire(1024)

    assert sleeps[0] <= 64, f"{sleeps[0]} sleeps for 512 parts: the limiter sleeps once per part"
    assert 0.4 <= t[0] <= 0.75, f"512 KiB at 1 MiB/s took {t[0]:.2f}s of throttling"


async def test_limiter_still_enforces_the_rate_with_coalescing():
    """Coalescing must not turn into 'never sleep': the global average is still the limit."""
    t = [0.0]

    async def sleep(d):
        t[0] += d

    tb = TokenBucket(1000, burst_seconds=0, clock=lambda: t[0], sleep=sleep)
    for _ in range(10):
        await tb.acquire(500)
    assert t[0] == pytest.approx(5.0, rel=0.01)  # 5000 bytes at 1000 B/s


async def test_limiter_set_rate_and_validation():
    tb = TokenBucket(100)
    tb.set_rate(None)
    assert await tb.acquire(10**6) == 0.0
    with pytest.raises(ValueError):
        tb.set_rate(0)


async def test_limiter_refunds_on_cancel():
    tb = TokenBucket(10, burst_seconds=0)
    task = asyncio.create_task(tb.acquire(1000))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tb._tokens > -50  # debt was refunded, not left to starve later callers


# ---------------------------------------------------------------- retry/backoff
def test_backoff_grows_is_bounded_and_resets():
    b = Backoff(RetryPolicy(base_delay=1, factor=2, max_delay=30, jitter=0, max_attempts=8))
    delays = [b.next_delay() for _ in range(8)]
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
    assert b.exhausted
    b.reset()
    assert b.attempts == 0 and b.next_delay() == 1


def test_backoff_jitter_stays_within_bounds():
    b = Backoff(RetryPolicy(base_delay=4, jitter=0.25, max_delay=100), rng=lambda: 0.0)
    assert b.next_delay() == pytest.approx(3.0)
    b = Backoff(RetryPolicy(base_delay=4, jitter=0.25, max_delay=100), rng=lambda: 1.0)
    assert b.next_delay() == pytest.approx(5.0)


def test_backoff_instances_are_independent():
    """Scoped backoff: one part's failures never raise another part's delay."""
    p = RetryPolicy(jitter=0)
    a, b = Backoff(p), Backoff(p)
    for _ in range(5):
        a.next_delay()
    assert b.next_delay() == p.base_delay


# ---------------------------------------------------------------- paths
@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "C:\\Windows\\x", "\\\\server\\share\\x", "a/CON", "a/nul.txt",
                                 "a/b:stream", "a/b?.txt", "", "a/ trailing ", "a/dot.", "a//..", "COM1"])  # fmt: skip
def test_unsafe_paths_rejected(bad, tmp_path):
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, bad)


def test_safe_join_keeps_inside_root(tmp_path):
    p = safe_join(tmp_path, "Photos\\2026/img.jpg")
    assert p == tmp_path.resolve() / "Photos" / "2026" / "img.jpg"
    assert split_relative("a/b") == ["a", "b"]


def test_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePathError):
        safe_join(root, "link/evil.txt")


def test_unique_sibling_and_topic_title(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert unique_sibling(f).name == "a (1).txt"
    t = sanitize_topic_title("x" * 300)
    assert len(t) <= 128 and t == sanitize_topic_title("x" * 300)
    assert sanitize_topic_title("x" * 300) != sanitize_topic_title("x" * 299 + "y")


# ---------------------------------------------------------------- planning
def test_volume_planning_and_rangeset():
    lim = UploadLimits(part_size=100, max_parts=4)
    vols = plan_volumes(1000, lim)
    assert [v.length for v in vols] == [400, 400, 200]
    assert vols[2].part_count == 2 and vols[2].part_range(1) == (900, 100)
    assert plan_volumes(0, lim)[0].part_count == 1
    rs = RangeSet({0, 1, 2, 5, 7, 8})
    assert rs.serialize() == "0-2,5,7-8"
    assert RangeSet.parse(rs.serialize()).contiguous_prefix() == 3
    assert RangeSet.parse("").serialize() == ""


def test_limits_are_dynamic_not_hardcoded():
    assert UploadLimits(max_parts=8000).max_volume_size == 2 * UploadLimits(max_parts=4000).max_volume_size


# ---------------------------------------------------------------- progress
def test_progress_tracks_acknowledged_bytes_speed_eta():
    t = [0.0]
    pt = ProgressTracker(1000, clock=lambda: t[0])
    for _ in range(4):
        t[0] += 1.0
        pt.add(100)
    snap: ProgressSnapshot = pt.snapshot()
    assert snap.done == 400 and snap.percent == 40.0
    assert snap.speed == pytest.approx(100, rel=0.35)
    assert snap.eta is not None and 4 < snap.eta < 9
    pt.rewind(0)
    assert pt.snapshot().done == 0
