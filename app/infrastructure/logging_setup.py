"""Structured JSON logging with mandatory redaction of secrets, phone numbers and codes."""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any

from ..domain.redact import redact


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        data = getattr(record, "data", None)
        if data:
            payload["data"] = _scrub(data)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_dir: Path, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_teloude", False):
            root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(log_dir / "teloude.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(SafeJsonFormatter())
    fh._teloude = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    root.setLevel(level)
    # Telethon logs connection details at DEBUG/INFO; keep it at WARNING and redacted anyway.
    logging.getLogger("telethon").setLevel(logging.WARNING)
