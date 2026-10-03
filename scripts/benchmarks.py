"""Deterministic benchmarks against the in-memory FAKE transport.

These numbers measure Teloude's own pipeline overhead and scheduling behaviour. They are NOT Telegram
throughput and must never be quoted as such.  Run:  python scripts/benchmarks.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.application.control import TransferControl  # noqa: E402
from app.application.pipeline import PipelineConfig, UploadPipeline  # noqa: E402
from app.domain.errors import TransientNetworkError  # noqa: E402
from app.domain.models import UploadLimits  # noqa: E402
from app.domain.planning import RangeSet, plan_volumes  # noqa: E402
from app.domain.progress import ProgressTracker  # noqa: E402
from app.domain.ratelimit import TokenBucket  # noqa: E402
from app.domain.retry import RetryPolicy  # noqa: E402
from app.testing.fake_gateway import FakeGateway  # noqa: E402

PART = 512 * 1024


class NullSink:
    def save(self, *_a):
        pass


async def run(path, size, gw, cfg, limiter=None, sink=None):
    plan = plan_volumes(size, UploadLimits(part_size=PART))[0]
    pipe = UploadPipeline(gw, limiter or TokenBucket(None), TransferControl(), cfg)
    t0 = time.perf_counter()
    await pipe.upload_volume(path=path, plan=plan, name="b", session=None, completed=RangeSet(), checkpoint=sink or NullSink(),
                             progress=ProgressTracker(size), base_done=0)  # fmt: skip
    return time.perf_counter() - t0, pipe


async def main() -> None:
    out = ["# Benchmarks (FAKE transport - not Telegram throughput)", ""]
    with tempfile.TemporaryDirectory() as td:
        size = 64 * 1024 * 1024
        path = os.path.join(td, "big.bin")
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        out += ["## Pipeline concurrency (64 MiB, 128 parts, fake 20 ms round trip per part)", "", "| concurrency | seconds | parts/s | max in flight |", "|---|---|---|---|"]
        for c in (1, 2, 4, 8):
            gw = FakeGateway(part_latency=0.02)
            t, p = await run(path, size, gw, PipelineConfig(concurrency=c))
            out.append(f"| {c} | {t:.2f} | {128 / t:.0f} | {gw.max_inflight} |")
        out += ["", "## Pipeline overhead with zero latency (pure Teloude cost, 64 MiB)", ""]
        t, p = await run(path, size, FakeGateway(), PipelineConfig(concurrency=4))
        out.append(f"{t:.2f} s  ({size / t / 1048576:.0f} MiB/s of local read + scheduling + fake copy)")
        out += ["", "## Speed limiter (16 MiB, 4 parts in flight)", "", "| limit | expected s | measured s |", "|---|---|---|"]
        small = 16 * 1024 * 1024
        for label, rate in (("unlimited", None), ("10 MB/s", 10 * 1048576), ("5 MB/s", 5 * 1048576), ("2 MB/s", 2 * 1048576)):
            gw = FakeGateway(part_latency=0.002)
            t, p = await run(path, small, gw, PipelineConfig(concurrency=4), TokenBucket(rate, burst_seconds=0))
            out.append(f"| {label} | {'-' if rate is None else f'{small / rate:.2f}'} | {t:.2f} |")
        out += ["", "## Retry behaviour (every 5th request fails; base delay 0.05 s, instant sleeps counted)", ""]
        state = {"n": 0}

        def hook(op, n):
            if op == "upload_part":
                state["n"] += 1
                if state["n"] % 5 == 0:
                    return TransientNetworkError("blip")

        slept: list[float] = []

        async def sleep(d):
            slept.append(d)
            await asyncio.sleep(0)

        gw = FakeGateway(part_latency=0.002, fail_hook=hook)
        t, p = await run(path, small, gw, PipelineConfig(concurrency=4, retry=RetryPolicy(base_delay=0.05, jitter=0), sleep=sleep))
        out.append(f"{p.diag.retries} retries, {p.diag.reconnects} reconnects (single-flight), completed in {t:.2f} s, longest single backoff chunk {max(slept):.2f} s")
        out += ["", "## Checkpoint cost (blocking sink, 100 ms per save) does not stall the pipeline", ""]

        class SlowSink:
            def save(self, *_a):
                time.sleep(0.1)

        t_fast, _ = await run(path, small, FakeGateway(part_latency=0.002), PipelineConfig(concurrency=4, checkpoint_interval=0.05), sink=NullSink())
        t_slow, p = await run(path, small, FakeGateway(part_latency=0.002), PipelineConfig(concurrency=4, checkpoint_interval=0.05), sink=SlowSink())
        out.append(f"fast sink {t_fast:.2f} s vs 100 ms-per-save sink {t_slow:.2f} s ({p.diag.checkpoints_saved} saves)")
        rs = RangeSet(set(range(0, 8000, 2)))
        t0 = time.perf_counter()
        for _ in range(100):
            RangeSet.parse(rs.serialize())
        out.append(f"\nCheckpoint (de)serialisation of 4000 scattered parts: {(time.perf_counter() - t0) * 10:.2f} ms each")
    text = "\n".join(out)
    print(text)
    (Path(__file__).resolve().parents[1] / "docs" / "BENCHMARKS.md").write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
