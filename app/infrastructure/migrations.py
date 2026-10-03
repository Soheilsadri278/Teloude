"""Ordered schema migrations. Never edit an applied migration; append a new one."""

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE storages (
    id             INTEGER PRIMARY KEY,
    uuid           TEXT NOT NULL UNIQUE,
    title          TEXT NOT NULL,
    tg_chat_id     INTEGER NOT NULL UNIQUE,
    tg_access_hash INTEGER,
    local_root     TEXT,
    created_at     REAL NOT NULL,
    last_synced_at REAL,
    status         TEXT NOT NULL DEFAULT 'active'
);

-- Telegram topics are flat. The *real* hierarchy lives here:
-- local folder -> logical folder (logical_path) -> Telegram topic (tg_topic_id)
CREATE TABLE folders (
    id           INTEGER PRIMARY KEY,
    storage_id   INTEGER NOT NULL REFERENCES storages(id) ON DELETE CASCADE,
    parent_id    INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    logical_path TEXT NOT NULL,
    tg_topic_id  INTEGER,
    UNIQUE (storage_id, logical_path)
);
CREATE INDEX ix_folders_parent ON folders(parent_id);

CREATE TABLE files (
    id           INTEGER PRIMARY KEY,
    storage_id   INTEGER NOT NULL REFERENCES storages(id) ON DELETE CASCADE,
    folder_id    INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    rel_path     TEXT NOT NULL,
    name         TEXT NOT NULL,
    ext          TEXT NOT NULL DEFAULT '',
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    sha256       TEXT,
    local_path   TEXT,
    state        TEXT NOT NULL,
    is_current   INTEGER NOT NULL DEFAULT 1,
    version      INTEGER NOT NULL DEFAULT 1,
    volumes_total INTEGER NOT NULL DEFAULT 1,
    backed_up_at REAL,
    last_error   TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE UNIQUE INDEX ux_files_current ON files(storage_id, rel_path) WHERE is_current = 1;
CREATE INDEX ix_files_hash   ON files(storage_id, sha256, size);
CREATE INDEX ix_files_folder ON files(folder_id);
CREATE INDEX ix_files_name   ON files(name COLLATE NOCASE);
CREATE INDEX ix_files_state  ON files(state);

CREATE TABLE telegram_messages (
    id           INTEGER PRIMARY KEY,
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    volume_index INTEGER NOT NULL,
    volumes_total INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    topic_id     INTEGER,
    size         INTEGER NOT NULL,
    created_at   REAL NOT NULL,
    UNIQUE (file_id, volume_index)
);

-- Persistent transfer state: the basis of resume and crash recovery.
CREATE TABLE transfers (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    run_id          INTEGER,
    file_id         INTEGER REFERENCES files(id) ON DELETE CASCADE,
    storage_id      INTEGER REFERENCES storages(id) ON DELETE CASCADE,
    state           TEXT NOT NULL,
    total_bytes     INTEGER NOT NULL DEFAULT 0,
    done_bytes      INTEGER NOT NULL DEFAULT 0,
    volume_index    INTEGER NOT NULL DEFAULT 0,
    volume_count    INTEGER NOT NULL DEFAULT 1,
    tg_file_id      INTEGER,
    completed_parts TEXT NOT NULL DEFAULT '',
    part_size       INTEGER,
    src_size        INTEGER,
    src_mtime_ns    INTEGER,
    finalizing      INTEGER NOT NULL DEFAULT 0,
    volume_messages TEXT NOT NULL DEFAULT '[]',
    session_restarts INTEGER NOT NULL DEFAULT 0,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    reconnect_count INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX ix_transfers_state ON transfers(state);
CREATE INDEX ix_transfers_file  ON transfers(file_id);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    storage_id  INTEGER REFERENCES storages(id) ON DELETE SET NULL,
    outcome     TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    total_files INTEGER NOT NULL DEFAULT 0,
    succeeded   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    cancelled   INTEGER NOT NULL DEFAULT 0,
    bytes_done  INTEGER NOT NULL DEFAULT 0,
    notified    INTEGER NOT NULL DEFAULT 0,
    summary     TEXT
);
""",
    ),
]
