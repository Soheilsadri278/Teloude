"""Bounded exponential backoff with jitter.

A ``Backoff`` instance is *scoped*: create one per unit of work (e.g. per upload part). Because a
fresh instance is created for each part, a successful recovery can never leak its attempt count into
an unrelated later failure (the "attempt=5 -> 30s wait forever" regression). ``reset()`` is also
available for long-lived users such as the reconnect loop.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    base_delay: float = 1.0
    factor: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.25  # +/- fraction
    max_attempts: int = 6  # consecutive failures of one unit of work before giving up


class Backoff:
    def __init__(self, policy: RetryPolicy = RetryPolicy(), rng: Callable[[], float] = random.random):
        self.policy = policy
        self._rng = rng
        self.attempts = 0

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.policy.max_attempts

    def next_delay(self) -> float:
        """Register one more failure and return the delay to wait before retrying."""
        self.attempts += 1
        p = self.policy
        raw = min(p.max_delay, p.base_delay * (p.factor ** (self.attempts - 1)))
        spread = 1.0 + p.jitter * (2.0 * self._rng() - 1.0)
        return max(0.0, min(p.max_delay, raw * spread))

    def reset(self) -> None:
        self.attempts = 0
