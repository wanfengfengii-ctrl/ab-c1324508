"""SQLite schema and connection management.

The service stores fractional-dose data as canonical decimal strings and
computes with fixed-point integers (micro-units, 1e-6 resolution) so that
repeated additions never drift and equality against the prescription is
exact. All state lives in this one file, which is bind-mounted as a
persistent Docker volume.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS courses (
    id            TEXT PRIMARY KEY,
    revision      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',
    channels_json TEXT NOT NULL,          -- canonical names, in request order
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_state (
    course_id     TEXT NOT NULL,
    channel       TEXT NOT NULL,
    prescribed_u  INTEGER NOT NULL,       -- fixed-point micro-units
    delivered_u   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (course_id, channel),
    FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS deliveries (
    id          TEXT NOT NULL,
    course_id   TEXT NOT NULL,
    base_rev    INTEGER NOT NULL,        -- expectedRevision the client sent
    increments_json TEXT NOT NULL,       -- canonical decimal string map
    result_rev  INTEGER NOT NULL,
    result_status TEXT NOT NULL,         -- course status right after first apply
    result_totals_json TEXT NOT NULL,    -- canonical totals map at result time
    accepted    INTEGER NOT NULL,        -- 1 applied, 0 permanently rejected
    created_at  REAL NOT NULL,
    PRIMARY KEY (course_id, id)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_course ON deliveries(course_id);
"""

# SQLite itself serializes writers: every state-changing transaction
# takes a BEGIN IMMEDIATE write lock, so contenders hit SQLITE_BUSY, wait
# on busy_timeout, and then re-read the freshest revision inside their
# own transaction -- a stale contender then fails the optimistic
# revision check instead of overwriting anything.
_init_lock = threading.Lock()
_initialized: set[str] = set()


def db_path() -> str:
    return os.environ.get("DOSIMETRY_DB", "/data/dosimetry.db")


def init_db(path: str | None = None) -> str:
    path = path or db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _init_lock:
        if path in _initialized:
            return path
        conn = connect(path)
        try:
            # `executescript` runs the WAL pragma and DDL atomically enough.
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()
        _initialized.add(path)
    return path


def _migrate(conn) -> None:
    """Tiny forward-only migration for dev databases created before
    ``deliveries.result_status`` existed. Fresh databases skip this."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(deliveries)")}
    if "result_status" not in cols:
        conn.execute("ALTER TABLE deliveries ADD COLUMN result_status TEXT NOT NULL DEFAULT 'active'")


def connect(path: str | None = None):
        path = path or db_path()
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # Autocommit-ish: we manage transactions explicitly with BEGIN
        # IMMEDIATE / COMMIT, and busy_timeout is the cross-process guard
        # for the Docker restart / multi-worker scenarios.
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


@contextmanager
def transaction(conn) -> Iterator[None]:
    """Serialize write transactions with BEGIN IMMEDIATE.

    At most one writer exists at a time across processes (SQLite guarantees
    this) and across threads within this process (the immediate lock fails
    fast for others, who retry at the service layer).
    """
    last_error: Exception | None = None
    for attempt in range(1000):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:  # database is locked
            last_error = exc
            time.sleep(0.002 * min(attempt, 50))
    else:
        raise last_error  # pragma: no cover - defensive
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pass
