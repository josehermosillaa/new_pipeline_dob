import json
import sqlite3
import time
from contextlib import contextmanager


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bins (
    bin TEXT PRIMARY KEY,
    source_order INTEGER NOT NULL,
    partition_no INTEGER NOT NULL,
    priority TEXT NOT NULL,
    house_no TEXT NOT NULL DEFAULT '',
    street_name TEXT NOT NULL DEFAULT '',
    borough TEXT NOT NULL DEFAULT '',
    block TEXT NOT NULL DEFAULT '',
    lot TEXT NOT NULL DEFAULT '',
    street_variants_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    response_json TEXT,
    last_filing_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY,
    bin TEXT NOT NULL REFERENCES bins(bin),
    job_filing_number TEXT NOT NULL,
    source_order INTEGER NOT NULL,
    priority TEXT NOT NULL,
    input_json TEXT NOT NULL,
    guid TEXT,
    job_json TEXT,
    search_status TEXT NOT NULL DEFAULT 'pending',
    pw1_status TEXT NOT NULL DEFAULT 'pending',
    pw1_json TEXT,
    zd1wd_status TEXT NOT NULL DEFAULT 'pending',
    zd1wd_json TEXT,
    portal_status TEXT NOT NULL DEFAULT 'pending',
    portal_json TEXT,
    normalized INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE(bin, job_filing_number)
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    filing_id INTEGER NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
    document_key TEXT NOT NULL,
    document_url TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    type_name TEXT NOT NULL DEFAULT '',
    status_label TEXT NOT NULL DEFAULT '',
    create_on TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    variants_json TEXT NOT NULL DEFAULT '[]',
    matched INTEGER NOT NULL DEFAULT 0,
    download_status TEXT NOT NULL DEFAULT 'skipped',
    download_url TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE(filing_id, document_key)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    level TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bins_work
    ON bins(status, next_attempt_at, priority, source_order);
CREATE INDEX IF NOT EXISTS idx_filings_work
    ON filings(search_status, next_attempt_at, priority, source_order);
CREATE INDEX IF NOT EXISTS idx_documents_work
    ON documents(download_status, next_attempt_at, id);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def initialize(conn):
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn, immediate=False):
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def set_metadata(conn, values):
    conn.executemany(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        [(key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()],
    )


def get_metadata(conn, key, default=None):
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return default


def event(conn, level, entity_type, entity_id, message):
    conn.execute(
        "INSERT INTO events(created_at, level, entity_type, entity_id, message) VALUES (?, ?, ?, ?, ?)",
        (time.time(), level, entity_type, str(entity_id), str(message)[:2000]),
    )


def summary(conn):
    result = {}
    for table in ("bins", "filings", "documents"):
        result[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    result["bin_status"] = dict(conn.execute(
        "SELECT status, COUNT(*) FROM bins GROUP BY status"
    ).fetchall())
    result["filing_endpoints"] = dict(conn.execute("""
        SELECT pw1_status || '/' || zd1wd_status || '/' || portal_status, COUNT(*)
        FROM filings GROUP BY 1
    """).fetchall())
    result["download_status"] = dict(conn.execute(
        "SELECT download_status, COUNT(*) FROM documents GROUP BY download_status"
    ).fetchall())
    return result
