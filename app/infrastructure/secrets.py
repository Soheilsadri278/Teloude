"""Protected secret storage.

* Windows: DPAPI (``CryptProtectData``) - bound to the signed-in Windows user, nothing in plaintext.
* Elsewhere (development only): refuses to persist unless ``TELOUDE_ALLOW_INSECURE_SECRETS=1``;
  otherwise secrets live in memory for the process lifetime only.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import sys
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("teloude.secrets")
_ENTROPY = b"Teloude/secret-store/v1"


class MemorySecretStore:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._d.get(name)

    def set(self, name: str, value: str) -> None:
        self._d[name] = value

    def delete(self, name: str) -> None:
        self._d.pop(name, None)


class _FileStore:
    suffix = ".bin"

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not name.replace("_", "").isalnum():
            raise ValueError("invalid secret name")
        return self.dir / f"{name}{self.suffix}"

    def _encode(self, raw: bytes) -> bytes:
        raise NotImplementedError

    def _decode(self, blob: bytes) -> bytes:
        raise NotImplementedError

    def get(self, name: str) -> str | None:
        p = self._path(name)
        if not p.exists():
            return None
        try:
            return self._decode(p.read_bytes()).decode("utf-8")
        except Exception:
            log.warning("could not decrypt secret %s (different Windows user?)", name)
            return None

    def set(self, name: str, value: str) -> None:
        p = self._path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(self._encode(value.encode("utf-8")))
        os.replace(tmp, p)  # atomic: a crash never leaves a half-written secret

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:  # type: ignore[type-arg]
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_protect(data: bytes, entropy: bytes = _ENTROPY) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    inb, _k1 = _blob(data)
    ent, _k2 = _blob(entropy)
    out = _DataBlob()
    if not crypt32.CryptProtectData(ctypes.byref(inb), "Teloude", ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_unprotect(blob: bytes, entropy: bytes = _ENTROPY) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    inb, _k1 = _blob(blob)
    ent, _k2 = _blob(entropy)
    out = _DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(inb), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


class DpapiSecretStore(_FileStore):
    suffix = ".dpapi"

    def _encode(self, raw: bytes) -> bytes:
        return dpapi_protect(raw)

    def _decode(self, blob: bytes) -> bytes:
        return dpapi_unprotect(blob)


class InsecureFileSecretStore(_FileStore):
    """DEV ONLY (non-Windows, explicit opt-in). Obfuscation, not encryption."""

    suffix = ".insecure"

    def _encode(self, raw: bytes) -> bytes:
        return base64.b64encode(raw)

    def _decode(self, blob: bytes) -> bytes:
        return base64.b64decode(blob)


def create_secret_store(directory: Path):  # type: ignore[no-untyped-def]
    if sys.platform == "win32":
        return DpapiSecretStore(directory)
    if os.environ.get("TELOUDE_ALLOW_INSECURE_SECRETS") == "1":
        log.warning("using INSECURE secret store (development only)")
        return InsecureFileSecretStore(directory)
    log.warning("no secure secret store on this platform: secrets are kept in memory only")
    return MemorySecretStore()
