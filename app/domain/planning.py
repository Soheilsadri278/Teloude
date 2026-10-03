"""Splitting a logical file into Telegram *volumes* and each volume into upload *parts*."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import UploadLimits


@dataclass(frozen=True)
class VolumePlan:
    index: int
    offset: int
    length: int
    part_size: int

    @property
    def part_count(self) -> int:
        return max(1, math.ceil(self.length / self.part_size))

    def part_range(self, part: int) -> tuple[int, int]:
        """Absolute (offset, length) of ``part`` within the source file."""
        start = self.offset + part * self.part_size
        end = min(self.offset + self.length, start + self.part_size)
        return start, end - start


def plan_volumes(size: int, limits: UploadLimits) -> list[VolumePlan]:
    """One volume per ``max_volume_size`` bytes. Empty files become one empty volume."""
    vol = limits.max_volume_size
    if size <= 0:
        return [VolumePlan(0, 0, 0, limits.part_size)]
    n = math.ceil(size / vol)
    return [VolumePlan(i, i * vol, min(vol, size - i * vol), limits.part_size) for i in range(n)]


def pick_part_size(volume_length: int, limits: UploadLimits) -> int:
    return limits.part_size


class RangeSet:
    """Compact set of completed part indexes, serialisable as '0-9,12,15-20'."""

    def __init__(self, items: set[int] | None = None):
        self._items: set[int] = set(items or ())

    def add(self, i: int) -> None:
        self._items.add(i)

    def __contains__(self, i: int) -> bool:
        return i in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(sorted(self._items))

    def clear(self) -> None:
        self._items.clear()

    def contiguous_prefix(self) -> int:
        """Number of parts 0..n-1 that are all complete (the 'ordered watermark')."""
        n = 0
        while n in self._items:
            n += 1
        return n

    def serialize(self) -> str:
        out: list[str] = []
        run: list[int] = []
        for i in sorted(self._items):
            if run and i == run[-1] + 1:
                run.append(i)
            else:
                if run:
                    out.append(_fmt(run))
                run = [i]
        if run:
            out.append(_fmt(run))
        return ",".join(out)

    @classmethod
    def parse(cls, text: str | None) -> RangeSet:
        rs = cls()
        if not text:
            return rs
        for chunk in text.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "-" in chunk:
                a, b = chunk.split("-", 1)
                rs._items.update(range(int(a), int(b) + 1))
            else:
                rs._items.add(int(chunk))
        return rs


def _fmt(run: list[int]) -> str:
    return str(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}"
