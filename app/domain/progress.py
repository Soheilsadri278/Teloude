"""Progress accounting based on bytes that were *actually acknowledged* as transferred."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from .models import ProgressSnapshot


class ProgressTracker:
    def __init__(
        self,
        total: int,
        initial: int = 0,
        window: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.total = total
        self.done = initial
        self._initial = initial
        self._window = window
        self._clock = clock
        self._start = clock()
        self._samples: deque[tuple[float, int]] = deque()

    def add(self, nbytes: int) -> None:
        now = self._clock()
        self.done = min(self.total, self.done + nbytes) if self.total else self.done + nbytes
        self._samples.append((now, nbytes))
        self._trim(now)

    def rewind(self, done: int) -> None:
        """Used only when a server-side upload session was lost and bytes must be re-sent."""
        self.done = done
        self._samples.clear()

    def _trim(self, now: float) -> None:
        while self._samples and now - self._samples[0][0] > self._window:
            self._samples.popleft()

    def speed(self) -> float:
        now = self._clock()
        self._trim(now)
        if not self._samples:
            return 0.0
        span = max(now - self._samples[0][0], 1e-3)
        span = min(max(span, 0.5), self._window)
        return sum(n for _, n in self._samples) / span

    def snapshot(self) -> ProgressSnapshot:
        spd = self.speed()
        remaining = max(0, self.total - self.done)
        eta = (remaining / spd) if spd > 1 and remaining else (0.0 if remaining == 0 else None)
        return ProgressSnapshot(
            done=self.done, total=self.total, speed=spd, eta=eta, elapsed=self._clock() - self._start
        )

    @property
    def transferred_this_session(self) -> int:
        return self.done - self._initial
