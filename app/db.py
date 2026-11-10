from collections.abc import AsyncIterator

import aiosqlite

from app.settings import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN ('admin','ingestor','user')),
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL,          -- stable per filename; one row per version
    version      INTEGER NOT NULL DEFAULT 1,
    uploaded_by  INTEGER REFERENCES users(user_id),
    filename     TEXT NOT NULL,
    file_sha256  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|ready|failed|superseded
    error        TEXT,
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    raw_path     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (document_id, version)
);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(file_sha256);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id      TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    version       INTEGER NOT NULL,
    page          INTEGER,
    type          TEXT,
    chunk_sha256  TEXT NOT NULL,
    asset_path    TEXT,
    access_group  TEXT,          -- placeholder, unused in v1
    pii_flags     TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    user        TEXT,
    query       TEXT,
    chunk_ids   TEXT,
    model       TEXT,
    tokens      INTEGER,
    latency_ms  INTEGER,
    trace_id    TEXT
);

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    config        TEXT,
    recall_at_k   REAL,
    answer_score  REAL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def init_db() -> None:
    path = get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA)
        try:  # databases created before tracing existed
            await db.execute("ALTER TABLE audit_log ADD COLUMN trace_id TEXT")
        except aiosqlite.OperationalError:
            pass  # column already there
        await db.commit()


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(get_settings().db_path) as db:
        db.row_factory = aiosqlite.Row
        yield db
