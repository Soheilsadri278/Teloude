"""Asynchronous token-bucket rate limiter.

Design notes
------------
* ``acquire`` *reserves* tokens synchronously (no await between read/modify/write, so it is
  race-free on one event loop) and then sleeps only for *its own* deficit. Many concurrent
  uploaders therefore share one global budget **without being serialised** behind a lock or a
  blocking ``time.sleep``.
* ``rate=None`` means unlimited: ``acquire`` returns without awaiting anything.
* **Sleeps are coalesced.** A 1 KiB part at 1 MiB/s is worth 1 ms of waiting. Sleeping for 1 ms is
  worse than not sleeping at all: the timer granularity of the Windows event loop (~15.6 ms) turns
  every one of those sleeps into a full tick, which slowed a 512 KiB / 1 MiB/s upload from 0.5 s to
  4.0 s (512 parts -> 512 sleeps -> 8 s of tick-rounded napping). Below ``min_sleep`` the bytes are
  simply *borrowed*: ``_tokens`` stays negative, a later caller (or this one, once the debt is worth
  waking up for) sleeps for the whole backlog, and the long-run average rate is unchanged. The most
  a transfer can run ahead of schedule is ``rate * min_sleep`` bytes - 20 ms worth by default.
* ``total_throttled_seconds`` counts *wall-clock seconds during which the bucket owed bytes*, not
  the sum of every caller's individual wait: four workers sleeping for the same 100 ms are 100 ms
  of throttling, not 400 ms.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    #: Windows' monotonic clock ticks every ~15.6 ms; anything shorter is not worth a wake-up.
    MIN_SLEEP = 0.02

    def __init__(
        self,
        rate: float | None,
        burst_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        min_sleep: float = MIN_SLEEP,
    ):
        self._clock = clock
        self._sleep = sleep
        self._burst_seconds = burst_seconds
        self._min_sleep = max(0.0, min_sleep)
        self._rate: float | None = None
        self._capacity = 0.0
        self._tokens = 0.0
        self._last = clock()
        self.total_throttled_seconds = 0.0
        self.set_rate(rate)

    @property
    def rate(self) -> float | None:
        return self._rate

    def set_rate(self, rate: float | None) -> None:
        if rate is not None and rate <= 0:
            raise ValueError("rate must be positive or None")
        self._refill()
        was_unlimited = self._rate is None
        self._rate = rate
        if rate is None:
            self._capacity = 0.0
            self._tokens = 0.0
        else:
            self._capacity = rate * self._burst_seconds
            self._tokens = self._capacity if was_unlimited else min(self._tokens, self._capacity)
        self._last = self._clock()

    def _refill(self) -> None:
        now = self._clock()
        delta = now - self._last
        self._last = now
        if self._rate is None or delta <= 0:
            return
        if self._tokens < 0:
            # Time the bucket spent in debt - capped at what it actually takes to repay it, so an
            # idle bucket that is merely behind does not book minutes of nobody-waiting time.
            self.total_throttled_seconds += min(delta, -self._tokens / self._rate)
        self._tokens = min(self._capacity, self._tokens + delta * self._rate)

    async def acquire(self, nbytes: int) -> float:
        """Wait until ``nbytes`` may be sent. Returns the seconds this caller waited."""
        if self._rate is None or nbytes <= 0:
            return 0.0
        self._refill()
        self._tokens -= nbytes  # may go negative: that is the "debt" the caller waits out
        if self._tokens >= 0:
            return 0.0
        wait = -self._tokens / self._rate
        if wait < self._min_sleep:
            return 0.0  # borrowed, not waited: see the module docstring
        try:
            await self._sleep(wait)
        except asyncio.CancelledError:
            self._tokens += nbytes  # refund so a cancelled caller does not starve others
            raise
        return wait
