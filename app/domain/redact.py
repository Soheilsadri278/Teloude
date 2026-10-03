"""Central secret redaction used by logging and by any text that may reach the UI or the database."""

from __future__ import annotations

import re
import threading

_lock = threading.Lock()
_secrets: set[str] = set()

# Telethon StringSession: version char + urlsafe base64, long.
_SESSION_RE = re.compile(r"\b1[A-Za-z0-9_\-+/=]{120,}")
_PHONE_RE = re.compile(r"(?<![\w.])\+\d[\d\s\-()]{6,17}\d")
_HEX_SECRET_RE = re.compile(r"\b(?:dd|ee)?[0-9a-fA-F]{32,}\b")
_KEYVAL_RE = re.compile(
    r"(?i)\b(api[_-]?hash|password|passwd|secret|session|code|token|phone)\b(\s*[=:]\s*)(['\"]?)([^\s,'\"}]+)"
)


def register_secret(value: str | None) -> None:
    """Remember an exact secret value so it is scrubbed from any future text."""
    if value and len(value) >= 4:
        with _lock:
            _secrets.add(value)


def clear_registered() -> None:
    with _lock:
        _secrets.clear()


def redact(text: object) -> str:
    s = str(text)
    with _lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        s = s.replace(secret, "<redacted>")
    s = _SESSION_RE.sub("<redacted-session>", s)
    s = _KEYVAL_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<redacted>", s)
    s = _PHONE_RE.sub("<redacted-phone>", s)
    s = _HEX_SECRET_RE.sub("<redacted-hex>", s)
    return s
