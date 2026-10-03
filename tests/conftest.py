from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.application.backup import (  # noqa: E402
    BackupService,
    DuplicateDecision,
    DuplicateInfo,
)
from app.application.pipeline import PipelineConfig  # noqa: E402
from app.application.resumable import ResumeService  # noqa: E402
from app.application.storage_service import StorageService  # noqa: E402
from app.domain.models import DuplicateAction, UploadLimits  # noqa: E402
from app.domain.ratelimit import TokenBucket  # noqa: E402
from app.domain.retry import RetryPolicy  # noqa: E402
from app.infrastructure.db import open_database  # noqa: E402
from app.infrastructure.repositories import Repos  # noqa: E402
from app.testing.fake_gateway import FakeGateway  # noqa: E402


class Env:
    """A fully wired backup environment on top of the in-memory fake transport."""

    def __init__(self, tmp: Path, gateway: FakeGateway | None = None, limits: UploadLimits | None = None):
        self.tmp = tmp
        self.db = open_database(tmp / "t.db")
        self.repos = Repos.create(self.db)
        self.gateway = gateway or FakeGateway(limits=limits or UploadLimits(part_size=1024, max_parts=4000))
        self.limiter = TokenBucket(None)
        self.sleeps: list[float] = []

        async def fast_sleep(d: float) -> None:  # retries must not slow the suite down
            self.sleeps.append(d)
            await asyncio.sleep(0)

        self.pcfg = PipelineConfig(
            concurrency=4, retry=RetryPolicy(base_delay=0.01, max_delay=0.05, jitter=0.0, max_attempts=3),
            progress_interval=0.02, checkpoint_interval=0.02, sleep=fast_sleep,
        )  # fmt: skip
        self.backup = BackupService(self.repos, self.gateway, self.limiter, lambda: self.pcfg)
        self.storage = StorageService(self.repos, self.gateway)
        self.resume_service = ResumeService(self.repos)
        self.src = tmp / "Photography"
        self.src.mkdir()

    async def new_storage(self, name: str = "Photography") -> int:
        return await self.storage.create_storage(name)

    def write(self, rel: str, data: bytes) -> Path:
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def decide(action: DuplicateAction, apply_all: bool = False):
    calls: list[DuplicateInfo] = []

    async def resolver(info: DuplicateInfo) -> DuplicateDecision:
        calls.append(info)
        return DuplicateDecision(action, apply_all)

    resolver.calls = calls  # type: ignore[attr-defined]
    return resolver


async def never_duplicate(info: DuplicateInfo) -> DuplicateDecision:
    raise AssertionError(f"unexpected duplicate prompt for {info.rel_path}")
