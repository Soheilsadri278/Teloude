"""Path safety for restore: remote-supplied names are untrusted input."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from .errors import UnsafePathError

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}  # fmt: skip
_INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def validate_component(name: str) -> str:
    """Validate a single path component against Windows *and* POSIX rules."""
    if name in ("", ".", ".."):
        raise UnsafePathError(f"invalid path component: {name!r}")
    if _INVALID_CHARS.search(name):
        raise UnsafePathError(f"invalid character in path component: {name!r}")
    if name.endswith((" ", ".")):
        raise UnsafePathError(f"component may not end with space or dot: {name!r}")
    stem = name.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        raise UnsafePathError(f"reserved Windows device name: {name!r}")
    if len(name) > 255:
        raise UnsafePathError("path component too long")
    return name


def split_relative(rel_path: str) -> list[str]:
    """Split an untrusted relative path (either separator) into validated components."""
    if not rel_path or not rel_path.strip():
        raise UnsafePathError("empty path")
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or normalized.startswith("//"):
        raise UnsafePathError(f"absolute path not allowed: {rel_path!r}")
    parts = PurePosixPath(normalized).parts
    if not parts:
        raise UnsafePathError("empty path")
    return [validate_component(p) for p in parts]


def safe_join(dest_root: str | os.PathLike[str], rel_path: str) -> Path:
    """Join ``rel_path`` under ``dest_root``; guarantees the result stays inside it."""
    parts = split_relative(rel_path)
    root = Path(dest_root).resolve()
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"path escapes destination: {rel_path!r}") from exc
    return candidate


def unique_sibling(path: Path) -> Path:
    """Return a non-existing sibling like ``name (1).ext`` (used by 'keep both')."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 10_000):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    raise UnsafePathError("could not find a free file name")


def sanitize_topic_title(title: str, limit: int = 128) -> str:
    """Telegram topic titles are limited to 128 chars. Keep them unique when truncated."""
    import hashlib

    title = " ".join(title.split())
    if len(title) <= limit:
        return title
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    return f"{title[: limit - 12].rstrip()}… {digest}"


def to_posix_rel(parts: list[str]) -> str:
    return "/".join(parts)
