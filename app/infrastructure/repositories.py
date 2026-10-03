"""Repositories over the SQLite database. SQL lives here and nowhere else."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..domain.models import FileState, RunOutcome, ScannedFile, TransferState
from .db import Database

Row = sqlite3.Row


def _now() -> float:
    return time.time()


class SettingsRepo:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return default if row is None else row["value"]

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.set(key, json.dumps(value))

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key=?", (key,))


class StorageRepo:
    def __init__(self, db: Database):
        self.db = db

    def add(self, uuid: str, title: str, chat_id: int, access_hash: int | None, local_root: str | None = None) -> int:
        return self.db.execute(
            "INSERT INTO storages(uuid,title,tg_chat_id,tg_access_hash,local_root,created_at) VALUES(?,?,?,?,?,?)",
            (uuid, title, chat_id, access_hash, local_root, _now()),
        ).lastrowid

    def get(self, storage_id: int) -> Row | None:
        return self.db.query_one("SELECT * FROM storages WHERE id=?", (storage_id,))

    def by_chat(self, chat_id: int) -> Row | None:
        return self.db.query_one("SELECT * FROM storages WHERE tg_chat_id=?", (chat_id,))

    def by_uuid(self, uuid: str) -> Row | None:
        return self.db.query_one("SELECT * FROM storages WHERE uuid=?", (uuid,))

    def list(self) -> list[Row]:
        return self.db.query("SELECT * FROM storages WHERE status='active' ORDER BY title COLLATE NOCASE")

    def set_local_root(self, storage_id: int, root: str) -> None:
        self.db.execute("UPDATE storages SET local_root=? WHERE id=?", (root, storage_id))

    def touch_synced(self, storage_id: int) -> None:
        self.db.execute("UPDATE storages SET last_synced_at=? WHERE id=?", (_now(), storage_id))

    def forget(self, storage_id: int) -> None:
        """Remove the *local* record only. The Telegram group and its files are untouched."""
        self.db.execute("DELETE FROM storages WHERE id=?", (storage_id,))

    def usage(self, storage_id: int) -> tuple[int, int]:
        r = self.db.query_one(
            "SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files WHERE storage_id=? AND is_current=1 AND state=?",
            (storage_id, FileState.BACKED_UP),
        )
        return int(r["c"]), int(r["s"])


@dataclass
class Folder:
    id: int
    storage_id: int
    parent_id: int | None
    name: str
    logical_path: str
    tg_topic_id: int | None


def _folder(r: Row) -> Folder:
    return Folder(r["id"], r["storage_id"], r["parent_id"], r["name"], r["logical_path"], r["tg_topic_id"])


class FolderRepo:
    def __init__(self, db: Database):
        self.db = db

    def ensure(self, storage_id: int, logical_path: str) -> Folder:
        """Get/create the folder row for ``logical_path`` (posix, e.g. 'Photos/Portraits') and ancestors."""
        parts = [p for p in logical_path.split("/") if p]
        parent: Folder | None = None
        with self.db.transaction():
            for i in range(len(parts)):
                lp = "/".join(parts[: i + 1])
                row = self.db.query_one("SELECT * FROM folders WHERE storage_id=? AND logical_path=?", (storage_id, lp))
                if row is None:
                    fid = self.db.execute(
                        "INSERT INTO folders(storage_id,parent_id,name,logical_path) VALUES(?,?,?,?)",
                        (storage_id, parent.id if parent else None, parts[i], lp),
                    ).lastrowid
                    row = self.db.query_one("SELECT * FROM folders WHERE id=?", (fid,))
                parent = _folder(row)  # type: ignore[arg-type]
        assert parent is not None
        return parent

    def get(self, folder_id: int) -> Folder | None:
        r = self.db.query_one("SELECT * FROM folders WHERE id=?", (folder_id,))
        return _folder(r) if r else None

    def by_path(self, storage_id: int, logical_path: str) -> Folder | None:
        r = self.db.query_one("SELECT * FROM folders WHERE storage_id=? AND logical_path=?", (storage_id, logical_path))
        return _folder(r) if r else None

    def by_topic(self, storage_id: int, topic_id: int) -> Folder | None:
        r = self.db.query_one("SELECT * FROM folders WHERE storage_id=? AND tg_topic_id=?", (storage_id, topic_id))
        return _folder(r) if r else None

    def set_topic(self, folder_id: int, topic_id: int) -> None:
        self.db.execute("UPDATE folders SET tg_topic_id=? WHERE id=?", (topic_id, folder_id))

    def list(self, storage_id: int) -> list[Folder]:
        return [_folder(r) for r in self.db.query("SELECT * FROM folders WHERE storage_id=? ORDER BY logical_path", (storage_id,))]


class FileRepo:
    def __init__(self, db: Database):
        self.db = db

    # -- reads -----------------------------------------------------------------------------
    def get(self, file_id: int) -> Row | None:
        return self.db.query_one("SELECT * FROM files WHERE id=?", (file_id,))

    def current_index(self, storage_id: int) -> dict[str, Row]:
        rows = self.db.query("SELECT * FROM files WHERE storage_id=? AND is_current=1", (storage_id,))
        return {r["rel_path"]: r for r in rows}

    def list_backed_up(self, storage_id: int, path_prefix: str | None = None) -> list[Row]:
        sql = "SELECT * FROM files WHERE storage_id=? AND is_current=1 AND state=?"
        params: list[Any] = [storage_id, FileState.BACKED_UP]
        if path_prefix:
            sql += " AND (rel_path=? OR rel_path LIKE ? ESCAPE '\\')"
            params += [path_prefix, _like_escape(path_prefix.rstrip("/")) + "/%"]
        return self.db.query(sql + " ORDER BY rel_path COLLATE NOCASE", params)

    def list_in_folder(self, folder_id: int) -> list[Row]:
        return self.db.query(
            "SELECT * FROM files WHERE folder_id=? AND is_current=1 AND state=? ORDER BY name COLLATE NOCASE",
            (folder_id, FileState.BACKED_UP),
        )

    def find_duplicates(self, storage_id: int, sha256: str, size: int, exclude_id: int | None = None) -> list[Row]:
        return self.db.query(
            "SELECT * FROM files WHERE storage_id=? AND sha256=? AND size=? AND is_current=1 AND state=? AND id<>?",
            (storage_id, sha256, size, FileState.BACKED_UP, exclude_id or -1),
        )

    def search(self, text: str, storage_id: int | None = None, ext: str | None = None, limit: int = 500) -> list[Row]:
        """Substring search over name, rel_path (=folder) and storage title; terms are AND-ed."""
        sql = (
            "SELECT f.*, s.title AS storage_title FROM files f JOIN storages s ON s.id=f.storage_id "
            "WHERE f.is_current=1 AND f.state=?"
        )
        params: list[Any] = [FileState.BACKED_UP]
        for term in text.split():
            sql += " AND (f.rel_path LIKE ? ESCAPE '\\' OR s.title LIKE ? ESCAPE '\\' OR f.ext LIKE ? ESCAPE '\\')"
            like = f"%{_like_escape(term)}%"
            params += [like, like, like]
        if storage_id is not None:
            sql += " AND f.storage_id=?"
            params.append(storage_id)
        if ext:
            sql += " AND f.ext=?"
            params.append(ext.lower().lstrip("."))
        sql += " ORDER BY f.name COLLATE NOCASE LIMIT ?"
        params.append(limit)
        return self.db.query(sql, params)

    def stats(self) -> dict[str, int]:
        r = self.db.query_one(
            "SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files WHERE is_current=1 AND state=?",
            (FileState.BACKED_UP,),
        )
        pend = self.db.scalar(
            "SELECT COUNT(*) FROM files WHERE is_current=1 AND state IN (?,?,?,?)",
            (FileState.PENDING, FileState.UPLOADING, FileState.FAILED, FileState.PERMANENT_FAILED),
        )
        return {"files": int(r["c"]), "bytes": int(r["s"]), "pending": int(pend or 0)}

    # -- writes ----------------------------------------------------------------------------
    def upsert_pending(self, storage_id: int, folder_id: int, f: ScannedFile) -> int:
        """Register a new or changed file as pending. A changed *backed-up* file becomes a new version;
        the previous version row is kept (superseded) so its Telegram messages remain restorable."""
        now = _now()
        with self.db.transaction():
            cur = self.db.query_one(
                "SELECT * FROM files WHERE storage_id=? AND rel_path=? AND is_current=1", (storage_id, f.rel_path)
            )
            version = 1
            if cur is not None:
                if cur["state"] == FileState.BACKED_UP:
                    self.db.execute(
                        "UPDATE files SET is_current=0, state=?, updated_at=? WHERE id=?",
                        (FileState.SUPERSEDED, now, cur["id"]),
                    )
                    version = cur["version"] + 1
                else:  # never uploaded successfully: update in place, drop stale hash
                    unchanged = cur["size"] == f.size and cur["mtime_ns"] == f.mtime_ns
                    self.db.execute(
                        "UPDATE files SET size=?, mtime_ns=?, sha256=?, local_path=?, state=?, folder_id=?, "
                        "last_error=NULL, updated_at=? WHERE id=?",
                        (f.size, f.mtime_ns, cur["sha256"] if unchanged else None, f.abs_path, FileState.PENDING,
                         folder_id, now, cur["id"]),
                    )  # fmt: skip
                    return int(cur["id"])
            return self.db.execute(
                "INSERT INTO files(storage_id,folder_id,rel_path,name,ext,size,mtime_ns,local_path,state,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (storage_id, folder_id, f.rel_path, f.name, f.ext, f.size, f.mtime_ns, f.abs_path,
                 FileState.PENDING, version, now, now),
            ).lastrowid  # fmt: skip

    def revert_if_identical(self, file_id: int, sha256: str, size: int, mtime_ns: int) -> bool:
        """A backed-up file was merely touched (new mtime, same bytes): restore the previous version as
        current instead of uploading identical content again."""
        row = self.get(file_id)
        if row is None:
            return False
        prev = self.db.query_one(
            "SELECT * FROM files WHERE storage_id=? AND rel_path=? AND state=? AND version<? ORDER BY version DESC LIMIT 1",
            (row["storage_id"], row["rel_path"], FileState.SUPERSEDED, row["version"]),
        )
        if prev is None or prev["sha256"] != sha256 or prev["size"] != size:
            return False
        with self.db.transaction():
            self.db.execute("DELETE FROM files WHERE id=?", (file_id,))
            self.db.execute(
                "UPDATE files SET is_current=1, state=?, mtime_ns=?, local_path=?, updated_at=? WHERE id=?",
                (FileState.BACKED_UP, mtime_ns, row["local_path"], _now(), prev["id"]),
            )
        return True

    def set_hash(self, file_id: int, sha256: str, size: int, mtime_ns: int) -> None:
        self.db.execute(
            "UPDATE files SET sha256=?, size=?, mtime_ns=?, updated_at=? WHERE id=?", (sha256, size, mtime_ns, _now(), file_id)
        )

    def set_state(self, file_id: int, state: FileState, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE files SET state=?, last_error=?, updated_at=? WHERE id=?", (state, error, _now(), file_id)
        )

    def touch_unchanged(self, file_id: int, mtime_ns: int, local_path: str | None = None) -> None:
        self.db.execute(
            "UPDATE files SET mtime_ns=?, local_path=COALESCE(?,local_path), updated_at=? WHERE id=?",
            (mtime_ns, local_path, _now(), file_id),
        )

    def mark_backed_up(self, file_id: int, messages: Iterable[tuple[int, int, int, int | None, int]]) -> None:
        """Atomically record every volume's Telegram message AND flip the file to BACKED_UP.

        ``messages``: (volume_index, volumes_total, message_id, topic_id, size)."""
        msgs = list(messages)
        now = _now()
        with self.db.transaction():
            self.db.execute("DELETE FROM telegram_messages WHERE file_id=?", (file_id,))
            for vi, vt, mid, topic, size in msgs:
                self.db.execute(
                    "INSERT INTO telegram_messages(file_id,volume_index,volumes_total,message_id,topic_id,size,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (file_id, vi, vt, mid, topic, size, now),
                )
            self.db.execute(
                "UPDATE files SET state=?, volumes_total=?, backed_up_at=?, last_error=NULL, updated_at=? WHERE id=?",
                (FileState.BACKED_UP, len(msgs), now, now, file_id),
            )

    def add_remote(self, storage_id: int, folder_id: int, rel_path: str, size: int, mtime_ns: int, sha256: str | None,
                   version: int, messages: list[tuple[int, int, int, int | None, int]]) -> int:  # fmt: skip
        """Insert/refresh a file discovered from Telegram (no local DB needed on this PC)."""
        name = rel_path.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name.strip(".") else ""
        now = _now()
        with self.db.transaction():
            cur = self.db.query_one(
                "SELECT id, sha256, state FROM files WHERE storage_id=? AND rel_path=? AND is_current=1", (storage_id, rel_path)
            )
            if cur is not None:
                if cur["sha256"] == sha256 and cur["state"] == FileState.BACKED_UP:
                    return int(cur["id"])
                self.db.execute("UPDATE files SET is_current=0, state=? WHERE id=?", (FileState.SUPERSEDED, cur["id"]))
            fid = self.db.execute(
                "INSERT INTO files(storage_id,folder_id,rel_path,name,ext,size,mtime_ns,sha256,local_path,state,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
                (storage_id, folder_id, rel_path, name, ext, size, mtime_ns, sha256, FileState.PENDING, version, now, now),
            ).lastrowid  # fmt: skip
            self.mark_backed_up(fid, messages)
            return fid

    def mark_remote_deleted(self, file_id: int) -> None:
        with self.db.transaction():
            self.db.execute("DELETE FROM telegram_messages WHERE file_id=?", (file_id,))
            self.db.execute(
                "UPDATE files SET state=?, is_current=0, updated_at=? WHERE id=?", (FileState.REMOTE_DELETED, _now(), file_id)
            )

    def reset_stuck_uploading(self) -> int:
        return self.db.execute(
            "UPDATE files SET state=?, updated_at=? WHERE state=?", (FileState.PENDING, _now(), FileState.UPLOADING)
        ).rowcount


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class MessageRepo:
    def __init__(self, db: Database):
        self.db = db

    def for_file(self, file_id: int) -> list[Row]:
        return self.db.query("SELECT * FROM telegram_messages WHERE file_id=? ORDER BY volume_index", (file_id,))


_TRANSFER_FIELDS = {
    "state", "total_bytes", "done_bytes", "volume_index", "volume_count", "tg_file_id", "completed_parts",
    "part_size", "src_size", "src_mtime_ns", "finalizing", "volume_messages", "session_restarts", "retry_count",
    "reconnect_count", "error",
}  # fmt: skip


class TransferRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, kind: str, file_id: int | None, storage_id: int | None, total_bytes: int, volume_count: int,
               run_id: int | None, src_size: int | None = None, src_mtime_ns: int | None = None) -> int:  # fmt: skip
        now = _now()
        return self.db.execute(
            "INSERT INTO transfers(kind,run_id,file_id,storage_id,state,total_bytes,volume_count,src_size,src_mtime_ns,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (kind, run_id, file_id, storage_id, TransferState.QUEUED, total_bytes, volume_count, src_size, src_mtime_ns, now, now),
        ).lastrowid  # fmt: skip

    def get(self, transfer_id: int) -> Row | None:
        return self.db.query_one("SELECT * FROM transfers WHERE id=?", (transfer_id,))

    def update(self, transfer_id: int, **fields: Any) -> None:
        bad = set(fields) - _TRANSFER_FIELDS
        if bad:
            raise ValueError(f"unknown transfer fields: {bad}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE transfers SET {sets}, updated_at=? WHERE id=?",  # noqa: S608 - keys are whitelisted
            (*[str(v) if isinstance(v, TransferState) else v for v in fields.values()], _now(), transfer_id),
        )

    def bump(self, transfer_id: int, column: str) -> None:
        if column not in {"retry_count", "reconnect_count", "session_restarts"}:
            raise ValueError(column)
        self.db.execute(f"UPDATE transfers SET {column}={column}+1, updated_at=? WHERE id=?", (_now(), transfer_id))  # noqa: S608

    def resumable_for_file(self, file_id: int) -> Row | None:
        """The checkpoint a later run should continue from, or ``None`` to start clean.

        A permanently failed transfer is deliberately excluded: retrying it unchanged can never
        succeed, so it must not silently hijack the next run.
        """
        return self.db.query_one(
            "SELECT * FROM transfers WHERE file_id=? AND kind='backup' AND state IN (?,?,?,?) "
            "ORDER BY id DESC LIMIT 1",
            (
                file_id,
                TransferState.INTERRUPTED,
                TransferState.FAILED,
                TransferState.RECOVERABLE_FAILED,
                TransferState.CANCELLED,
            ),
        )

    def recoverable_for_storage(self, storage_id: int) -> list[Row]:
        """Transfers of this storage that a Resume action can continue, worst (oldest) first."""
        return self.db.query(
            "SELECT t.*, f.rel_path, f.name, f.local_path, f.state AS file_state "
            "FROM transfers t JOIN files f ON f.id=t.file_id "
            "WHERE t.storage_id=? AND t.kind='backup' AND t.state IN (?,?,?,?) AND f.state<>? "
            "ORDER BY t.id",
            (
                storage_id,
                TransferState.INTERRUPTED,
                TransferState.FAILED,
                TransferState.RECOVERABLE_FAILED,
                TransferState.CANCELLED,
                FileState.BACKED_UP,
            ),
        )

    def count_recoverable(self, storage_id: int) -> int:
        return len(self.recoverable_for_storage(storage_id))

    def resumable_or_active_for_file(self, file_id: int) -> Row | None:
        return self.db.query_one("SELECT * FROM transfers WHERE file_id=? AND kind='backup' ORDER BY id DESC LIMIT 1", (file_id,))

    def mark_all_active_interrupted(self) -> list[Row]:
        """Crash recovery: anything that claimed to be running when we died is merely *interrupted*."""
        active = [str(s) for s in TransferState if s.is_active]
        marks = ",".join("?" * len(active))
        with self.db.transaction():
            rows = self.db.query(f"SELECT * FROM transfers WHERE state IN ({marks})", active)  # noqa: S608
            self.db.execute(
                f"UPDATE transfers SET state=?, updated_at=? WHERE state IN ({marks})",  # noqa: S608
                (TransferState.INTERRUPTED, _now(), *active),
            )
        return rows

    def recent(self, limit: int = 200) -> list[Row]:
        return self.db.query(
            "SELECT t.*, f.rel_path FROM transfers t LEFT JOIN files f ON f.id=t.file_id ORDER BY t.id DESC LIMIT ?", (limit,)
        )

    def delete_for_file(self, file_id: int) -> None:
        self.db.execute("DELETE FROM transfers WHERE file_id=? AND kind='backup'", (file_id,))


class RunRepo:
    def __init__(self, db: Database):
        self.db = db

    def start(self, kind: str, storage_id: int | None) -> int:
        return self.db.execute(
            "INSERT INTO runs(kind,storage_id,outcome,started_at) VALUES(?,?,?,?)",
            (kind, storage_id, RunOutcome.RUNNING, _now()),
        ).lastrowid

    def finish(self, run_id: int, outcome: RunOutcome, total: int, ok: int, failed: int, skipped: int, cancelled: int,
               bytes_done: int, summary: str = "") -> None:  # fmt: skip
        self.db.execute(
            "UPDATE runs SET outcome=?, finished_at=?, total_files=?, succeeded=?, failed=?, skipped=?, cancelled=?, "
            "bytes_done=?, summary=? WHERE id=?",
            (outcome, _now(), total, ok, failed, skipped, cancelled, bytes_done, summary, run_id),
        )

    def get(self, run_id: int) -> Row | None:
        return self.db.query_one("SELECT * FROM runs WHERE id=?", (run_id,))

    def claim_notification(self, run_id: int) -> bool:
        """Atomically mark as notified; returns True only for the first caller (no duplicates)."""
        return self.db.execute("UPDATE runs SET notified=1 WHERE id=? AND notified=0", (run_id,)).rowcount == 1

    def recent(self, limit: int = 20) -> list[Row]:
        return self.db.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    def abandon_running(self) -> int:
        """A run still 'running' at startup died with the process."""
        return self.db.execute(
            "UPDATE runs SET outcome=?, finished_at=?, notified=1 WHERE outcome=?",
            (RunOutcome.PARTIAL, _now(), RunOutcome.RUNNING),
        ).rowcount


@dataclass
class Repos:
    settings: SettingsRepo
    storages: StorageRepo
    folders: FolderRepo
    files: FileRepo
    messages: MessageRepo
    transfers: TransferRepo
    runs: RunRepo

    db: Database | None = None

    @classmethod
    def create(cls, db: Database) -> Repos:
        return cls(
            SettingsRepo(db), StorageRepo(db), FolderRepo(db), FileRepo(db), MessageRepo(db), TransferRepo(db),
            RunRepo(db), db,
        )  # fmt: skip

    def db_transaction(self):  # type: ignore[no-untyped-def]
        assert self.db is not None
        return self.db.transaction()
