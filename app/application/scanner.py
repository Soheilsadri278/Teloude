"""Recursive folder scanner + streaming hasher.

Scanning is metadata-only (``os.scandir`` + the stat info it already has): it never reads file
contents. Hashing happens lazily, once, right before upload, and is cached by (size, mtime_ns).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

from ..domain.errors import TransferCancelled
from ..domain.models import FileState, ScannedFile, ScanResult

DEFAULT_IGNORE = ("Thumbs.db", "desktop.ini", "~$*", "*.teloude-part", ".DS_Store")
HASH_CHUNK = 4 * 1024 * 1024


def _long(path: str) -> str:
    """Prefix for Windows long paths (> MAX_PATH)."""
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        p = os.path.abspath(path)
        return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return path


def walk_files(
    root: str,
    ignore: tuple[str, ...] = DEFAULT_IGNORE,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> tuple[list[ScannedFile], list[tuple[str, str]]]:
    root_path = Path(root)
    top = root_path.name or root_path.drive.rstrip(":\\/") or "root"
    files: list[ScannedFile] = []
    errors: list[tuple[str, str]] = []
    stack: list[tuple[str, str]] = [(str(root_path), top)]
    while stack:
        if should_stop and should_stop():
            raise TransferCancelled("scan cancelled")
        directory, rel = stack.pop()
        try:
            with os.scandir(_long(directory)) as it:
                entries = list(it)
        except OSError as exc:
            errors.append((directory, str(exc)))
            continue
        for entry in entries:
            name = entry.name
            if any(fnmatch.fnmatch(name, pat) for pat in ignore):
                continue
            try:
                if entry.is_symlink():
                    continue  # never follow links: avoids loops and surprise escapes
                if entry.is_dir(follow_symlinks=False):
                    stack.append((os.path.join(directory, name), f"{rel}/{name}"))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    files.append(ScannedFile(f"{rel}/{name}", os.path.join(directory, name), st.st_size, st.st_mtime_ns))
            except OSError as exc:
                errors.append((os.path.join(directory, name), str(exc)))
        if on_progress:
            on_progress(len(files))
    files.sort(key=lambda f: f.rel_path.lower())
    return files, errors


def classify(
    scanned: list[ScannedFile], index: Mapping[str, sqlite3.Row], errors: list[tuple[str, str]] | None = None
) -> ScanResult:
    """Compare a scan with the DB. Never proposes deleting anything anywhere."""
    res = ScanResult(errors=list(errors or []))
    seen: set[str] = set()
    done_states = {FileState.BACKED_UP, FileState.DUPLICATE_SKIPPED}
    for f in scanned:
        seen.add(f.rel_path)
        row = index.get(f.rel_path)
        if row is None:
            res.new.append(f)
        elif row["state"] in done_states:
            if row["size"] == f.size and row["mtime_ns"] == f.mtime_ns:
                res.unchanged.append(f)
            else:
                res.changed.append(f)
        elif row["state"] == FileState.PERMANENT_FAILED:
            # A permanent failure is only worth another attempt once something actually changed
            # (the user fixed permissions, replaced the file, ...). Retrying it unchanged would
            # fail the same way on every single run.
            if row["size"] == f.size and row["mtime_ns"] == f.mtime_ns:
                res.blocked.append(f.rel_path)
            else:
                res.new.append(f)
        else:  # pending / uploading / recoverably failed from an earlier run: try again
            res.new.append(f)
    res.missing_locally = sorted(p for p, r in index.items() if p not in seen and r["state"] == FileState.BACKED_UP)
    return res


def scan_folder(
    root: str,
    index: Mapping[str, sqlite3.Row],
    ignore: tuple[str, ...] = DEFAULT_IGNORE,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ScanResult:
    files, errors = walk_files(root, ignore, should_stop, on_progress)
    return classify(files, index, errors)


def hash_file(path: str, should_stop: Callable[[], bool] | None = None) -> tuple[str, int, int]:
    """SHA-256 in a single streaming pass. Returns (hex, size_read, mtime_ns_after).

    ``size_read``/``mtime_ns`` come from the *same* open handle so callers can detect a file that
    changed while it was being read."""
    h = hashlib.sha256()
    total = 0
    with open(_long(path), "rb") as fh:
        while True:
            if should_stop and should_stop():
                raise TransferCancelled("hashing cancelled")
            chunk = fh.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
        st = os.fstat(fh.fileno())
    return h.hexdigest(), total, st.st_mtime_ns
