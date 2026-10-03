from __future__ import annotations

import logging
import os

import pytest

from app.application.backup import RunSummary
from app.application.metadata import VolumeMeta, decode_caption, encode_caption
from app.application.notifications import NotificationService, describe
from app.application.scanner import classify, hash_file, scan_folder, walk_files
from app.domain.errors import PermanentTransferError
from app.domain.models import FileState, RunOutcome, ScannedFile
from app.domain.redact import clear_registered, redact, register_secret
from app.infrastructure.db import Database, open_database
from app.infrastructure.logging_setup import configure_logging
from app.infrastructure.repositories import Repos


@pytest.fixture
def repos(tmp_path):
    return Repos.create(open_database(tmp_path / "a.db"))


def sf(rel, size=10, mtime=1):
    return ScannedFile(rel, "/x/" + rel, size, mtime)


# ------------------------------------------------------------------ database
def test_migrations_wal_and_pre_migration_backup(tmp_path):
    db = open_database(tmp_path / "x.db")
    assert db.schema_version >= 1
    assert db.scalar("PRAGMA journal_mode") == "wal"
    for t in ("storages", "folders", "files", "telegram_messages", "transfers", "settings", "runs"):
        assert db.scalar("SELECT name FROM sqlite_master WHERE name=?", (t,)) == t
    db.close()
    db2 = Database(tmp_path / "x.db")
    from app.infrastructure import migrations

    original = list(migrations.MIGRATIONS)
    try:
        migrations.MIGRATIONS.append((99, "CREATE TABLE later(a);"))
        import app.infrastructure.db as dbmod

        dbmod.MIGRATIONS = migrations.MIGRATIONS
        db2.migrate()
    finally:
        migrations.MIGRATIONS[:] = original
    assert db2.schema_version == 99
    assert (tmp_path / "x.db.bak-pre-v99").exists()  # state preserved before touching the schema


def test_corrupt_database_is_quarantined_not_destroyed(tmp_path):
    p = tmp_path / "bad.db"
    p.write_bytes(b"this is definitely not an sqlite database" * 100)
    db = open_database(p)
    assert db.quarantined is not None and db.quarantined.exists()
    assert db.quarantined.read_bytes().startswith(b"this is definitely")  # original bytes kept for forensics
    assert db.scalar("SELECT COUNT(*) FROM storages") == 0  # fresh usable DB


def test_transaction_rollback_and_nesting(tmp_path):
    db = open_database(tmp_path / "t.db")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("INSERT INTO settings VALUES('a','1')")
            with db.transaction():
                db.execute("INSERT INTO settings VALUES('b','2')")
            raise RuntimeError
    assert db.scalar("SELECT COUNT(*) FROM settings") == 0
    with db.transaction():
        with db.transaction():
            db.execute("INSERT INTO settings VALUES('c','3')")
    assert db.scalar("SELECT COUNT(*) FROM settings") == 1


# ------------------------------------------------------------------ repositories
def test_hierarchy_and_versions(repos):
    sid = repos.storages.add("u1", "Teloude - Photography", -1001, 5)
    leaf = repos.folders.ensure(sid, "Photography/Portraits/2026")
    assert [f.logical_path for f in repos.folders.list(sid)] == [
        "Photography", "Photography/Portraits", "Photography/Portraits/2026",
    ]  # fmt: skip
    assert leaf.parent_id is not None
    fid = repos.files.upsert_pending(sid, leaf.id, sf("Photography/Portraits/2026/a.jpg"))
    repos.files.mark_backed_up(fid, [(0, 1, 77, 9, 10)])
    assert repos.files.get(fid)["state"] == FileState.BACKED_UP
    new = repos.files.upsert_pending(sid, leaf.id, sf("Photography/Portraits/2026/a.jpg", size=11, mtime=2))
    assert new != fid
    old = repos.files.get(fid)
    assert old["state"] == FileState.SUPERSEDED and old["is_current"] == 0  # old version kept, not deleted
    assert repos.files.get(new)["version"] == 2
    assert len(repos.messages.for_file(fid)) == 1


def test_duplicate_search_and_stats(repos):
    sid = repos.storages.add("u", "Teloude - Docs", -2, 1)
    f = repos.folders.ensure(sid, "Docs/Sub")
    a = repos.files.upsert_pending(sid, f.id, sf("Docs/Sub/Report_2026.PDF"))
    b = repos.files.upsert_pending(sid, f.id, sf("Docs/Sub/other.txt"))
    repos.files.set_hash(a, "h1", 10, 1)
    repos.files.set_hash(b, "h1", 10, 1)
    repos.files.mark_backed_up(a, [(0, 1, 1, 1, 10)])
    assert [r["id"] for r in repos.files.find_duplicates(sid, "h1", 10, exclude_id=b)] == [a]
    repos.files.mark_backed_up(b, [(0, 1, 2, 1, 10)])
    assert [r["name"] for r in repos.files.search("report sub")] == ["Report_2026.PDF"]  # AND terms; name+folder
    assert [r["name"] for r in repos.files.search("docs", ext="txt")] == ["other.txt"]
    assert repos.files.search("100%_") == []  # LIKE wildcards are escaped
    assert repos.files.stats()["files"] == 2


def test_revert_if_identical_for_touched_files(repos):
    sid = repos.storages.add("u", "T", -3, 1)
    f = repos.folders.ensure(sid, "R")
    a = repos.files.upsert_pending(sid, f.id, sf("R/x.bin", 5, 1))
    repos.files.set_hash(a, "same", 5, 1)
    repos.files.mark_backed_up(a, [(0, 1, 1, 1, 5)])
    b = repos.files.upsert_pending(sid, f.id, sf("R/x.bin", 5, 999))  # touched: new mtime only
    assert repos.files.revert_if_identical(b, "same", 5, 999) is True
    cur = repos.files.get(a)
    assert cur["is_current"] == 1 and cur["state"] == FileState.BACKED_UP and cur["mtime_ns"] == 999


def test_notification_claim_is_atomic(repos):
    rid = repos.runs.start("backup", None)
    assert repos.runs.claim_notification(rid) is True
    assert repos.runs.claim_notification(rid) is False


# ------------------------------------------------------------------ scanner
def test_scan_classification_never_proposes_deletion(tmp_path, repos):
    root = tmp_path / "Photos"
    (root / "sub").mkdir(parents=True)
    (root / "a.jpg").write_bytes(b"1")
    (root / "sub" / "b.jpg").write_bytes(b"22")
    (root / "Thumbs.db").write_bytes(b"x")
    (root / "ok.teloude-part").write_bytes(b"x")
    sid = repos.storages.add("u", "T", -4, 1)
    files, errors = walk_files(str(root))
    assert [f.rel_path for f in files] == ["Photos/a.jpg", "Photos/sub/b.jpg"] and not errors
    res = scan_folder(str(root), repos.files.current_index(sid))
    assert len(res.new) == 2 and not res.changed
    for f in files:
        fid = repos.files.upsert_pending(sid, repos.folders.ensure(sid, "Photos").id, f)
        repos.files.mark_backed_up(fid, [(0, 1, fid, 1, f.size)])
    res = scan_folder(str(root), repos.files.current_index(sid))
    assert len(res.unchanged) == 2 and not res.pending
    (root / "a.jpg").write_bytes(b"changed!")
    os.remove(root / "sub" / "b.jpg")
    res = scan_folder(str(root), repos.files.current_index(sid))
    assert [f.rel_path for f in res.changed] == ["Photos/a.jpg"]
    assert res.missing_locally == ["Photos/sub/b.jpg"]  # reported only


def test_scan_skips_symlinks_and_hash_is_streaming(tmp_path):
    root = tmp_path / "R"
    root.mkdir()
    big = root / "big.bin"
    big.write_bytes(os.urandom(5 * 1024 * 1024 + 123))
    try:
        (root / "link").symlink_to(big)
    except OSError:
        pass
    files, _ = walk_files(str(root))
    assert [f.name for f in files] == ["big.bin"]
    import hashlib

    sha, size, mtime = hash_file(str(big))
    assert sha == hashlib.sha256(big.read_bytes()).hexdigest() and size == big.stat().st_size and mtime == big.stat().st_mtime_ns


def test_classify_retries_unfinished_files():
    class R(dict):
        pass

    idx = {"r/a": {"state": FileState.FAILED, "size": 1, "mtime_ns": 1}}
    res = classify([ScannedFile("r/a", "/r/a", 1, 1)], idx)
    assert len(res.new) == 1  # failed files are retried, not forgotten


# ------------------------------------------------------------------ metadata, redaction, logging, notifications
def test_caption_roundtrip_and_limit():
    m = VolumeMeta("Photos/ä ü/日本.jpg", "ab" * 32, 123, 456, 1, 3, 2, "uid")
    assert decode_caption(encode_caption(m, 1024)) == m
    assert decode_caption("hello") is None and decode_caption("teloude:v1\n{bad") is None
    with pytest.raises(PermanentTransferError):
        encode_caption(VolumeMeta("x/" * 600, "h", 1, 1, 0, 1, 1, "u"), 1024)


def test_redaction_covers_all_secret_kinds():
    clear_registered()
    register_secret("my-api-hash-value")
    session = "1" + "A" * 200
    text = f"api_hash=my-api-hash-value phone=+989121234567 code: 12345 password=hunter2 {session} secret=ee{'ab' * 16} dd{'cd' * 16}"
    out = redact(text)
    for leaked in ("my-api-hash-value", "989121234567", "12345", "hunter2", session, "abab", "cdcd"):
        assert leaked not in out
    clear_registered()


def test_log_file_never_contains_secrets(tmp_path):
    clear_registered()
    register_secret("SUPERSECRETHASH123")
    configure_logging(tmp_path)
    log = logging.getLogger("teloude.test")
    log.error("login failed api_hash=SUPERSECRETHASH123 phone +491701234567 code=99999")
    log.info("diag", extra={"data": {"proxy_secret": "SUPERSECRETHASH123", "retries": 3, "note": "password: pw1234"}})
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "teloude.log").read_text()
    assert "retries" in text  # diagnostics still useful
    for leaked in ("SUPERSECRETHASH123", "491701234567", "99999", "pw1234"):
        assert leaked not in text
    clear_registered()


def test_notifications_dedup_and_cancel_is_not_success(repos):
    got = []
    svc = NotificationService(repos.runs, got.append)
    rid = repos.runs.start("backup", None)
    ok = RunSummary(rid, "backup", 1, RunOutcome.COMPLETED, total=2, succeeded=2, bytes_done=2048)
    assert svc.notify_run(ok) is True and svc.notify_run(ok) is False  # no duplicates
    assert len(got) == 1 and got[0].kind == "success"
    cancelled = RunSummary(repos.runs.start("backup", None), "backup", 1, RunOutcome.CANCELLED, succeeded=1)
    assert describe(cancelled) is None and svc.notify_run(cancelled) is False
    partial = describe(RunSummary(3, "restore", 1, RunOutcome.PARTIAL, total=5, succeeded=3, failed=2))
    assert partial and "partially" in partial.title and partial.kind == "warning"
    failed = describe(RunSummary(4, "backup", 1, RunOutcome.FAILED, errors=["network"]))
    assert failed and failed.kind == "error"
