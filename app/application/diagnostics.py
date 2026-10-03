"""Secret-free counters that make production upload problems diagnosable."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


@dataclass
class TransferDiagnostics:
    parts_total: int = 0
    parts_uploaded: int = 0
    bytes_uploaded: int = 0
    upload_requests: int = 0  # every attempt, including retries
    retries: int = 0
    reconnects: int = 0
    flood_waits: int = 0
    session_restarts: int = 0
    inflight: int = 0
    max_inflight: int = 0
    checkpoints_saved: int = 0
    checkpoint_parts: int = 0  # parts recorded in the latest checkpoint
    throttled_seconds: float = 0.0
    started: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict[str, float | int]:
        d = asdict(self)
        d["elapsed_s"] = round(time.monotonic() - d.pop("started"), 3)
        d["throttled_seconds"] = round(d["throttled_seconds"], 3)
        mb = self.bytes_uploaded / (1024 * 1024)
        d["avg_mib_per_s"] = round(mb / d["elapsed_s"], 2) if d["elapsed_s"] > 0 else 0.0
        return d
