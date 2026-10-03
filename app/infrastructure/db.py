"""SQLite access: WAL, migrations (with pre-migration backup) and safe corruption handling."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .migrations import MIGRATIONS

log = logging.getLogger("teloude.db")


@dataclass
class ExecResult:
    lastrowid: int
    rowcount: int


class Database:
    """Single shared connection guarded by an RLock.

    Writes are tiny and local, so one connection with explicit ``transaction()`` blocks is both
    simple and fast. Callers batch many statements in one transaction when throughput matters
    (the scanner does this); the transfer path commits only on checkpoints, not per byte/part.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.quarantined: Path | None = None
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")

    # -- basic API -------------------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> ExecResult:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return ExecResult(cur.lastrowid or 0, cur.rowcount)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            try:
                if self.path != ":memory:":
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    # -- migrations -----------------------------------------------------------------------
    @property
    def schema_version(self) -> int:
        return int(self.scalar("PRAGMA user_version") or 0)

    def migrate(self) -> None:
        current = self.schema_version
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            if current > 0 and self.path != ":memory:":
                self._backup(f"pre-v{version}")
            log.info("applying migration %s", version)
            with self._lock:
                self._conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")
            current = version

    def _backup(self, label: str) -> None:
        dest = Path(f"{self.path}.bak-{label}")
        target = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._conn.backup(target)
        finally:
            target.close()


def open_database(path: str | Path) -> Database:
    """Open (and migrate) the database. A corrupt file is quarantined, never deleted."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    quarantined: Path | None = None
    if path.exists() and path.stat().st_size > 0:
        ok = True
        try:
            probe = sqlite3.connect(str(path))
            try:
                row = probe.execute("PRAGMA quick_check").fetchone()
                ok = bool(row) and row[0] == "ok"
            finally:
                probe.close()
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            quarantined = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            log.error("database failed integrity check; moving it to %s", quarantined)
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(path) + suffix)
                if src.exists():
                    shutil.move(str(src), str(quarantined) + suffix)
    db = Database(path)
    db.quarantined = quarantined
    db.migrate()
    return db
