import os
import sqlite3
import time
from pathlib import Path
from typing import Optional, Union

import appdirs

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('user', 'subreddit')),
    name TEXT NOT NULL COLLATE NOCASE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'banned')),
    sort TEXT,
    time TEXT,
    filter_gender TEXT,
    min_score INTEGER,
    post_limit INTEGER,
    extra_args TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_run_at TEXT,
    last_status TEXT,
    last_files INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('ignore_user', 'exclude_post', 'skip_subreddit', 'skip_domain')),
    value TEXT NOT NULL COLLATE NOCASE,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, value)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources (id) ON DELETE SET NULL,
    source_kind TEXT NOT NULL,
    source_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
    files_new INTEGER,
    files_skipped INTEGER,
    command TEXT,
    log_path TEXT,
    output_tail TEXT
);

CREATE INDEX IF NOT EXISTS runs_source ON runs (source_id, started_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Posts found in subreddit downloads, parsed from file names; the basis for candidate users
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY,
    author TEXT NOT NULL COLLATE NOCASE,
    subreddit TEXT NOT NULL COLLATE NOCASE,
    score INTEGER,
    posted_at TEXT,
    found_at TEXT NOT NULL DEFAULT (datetime('now')),
    run_id INTEGER REFERENCES runs (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS posts_author ON posts (author);

ALTER TABLE runs ADD COLUMN posts_found INTEGER;

CREATE TABLE IF NOT EXISTS dismissed_authors (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    reason TEXT,
    dismissed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Where each post's files are, so the UI can preview them. Paths are absolute, as found when recorded.
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS post_files (
    post_id TEXT NOT NULL REFERENCES posts (post_id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    PRIMARY KEY (post_id, path)
);
"""

# Files moved aside when their author was ignored, so they can be reviewed, deleted by hand, or restored
_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS set_aside (
    id INTEGER PRIMARY KEY,
    author TEXT NOT NULL COLLATE NOCASE,
    post_id TEXT,
    from_path TEXT NOT NULL,
    to_path TEXT NOT NULL,
    moved_at TEXT NOT NULL DEFAULT (datetime('now')),
    restored_at TEXT,
    gone_at TEXT
);

CREATE INDEX IF NOT EXISTS set_aside_author ON set_aside (author);
"""

# Internal state the server keeps between restarts (e.g. when the schedule last fired); not user settings
_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# The server's download queue, kept in the DB so waiting and interrupted downloads survive restarts
_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    time TEXT,
    trigger TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'finished')),
    queued_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    run_id INTEGER,
    status TEXT,
    files_new INTEGER,
    reason TEXT,
    resumed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS jobs_state ON jobs (state, id);
"""

# What started each run and how many files it couldn't get; indexes for the Runs and Latest pages
_SCHEMA_V7 = """
ALTER TABLE runs ADD COLUMN trigger TEXT;
ALTER TABLE runs ADD COLUMN files_failed INTEGER;
ALTER TABLE runs ADD COLUMN files_retry INTEGER;
ALTER TABLE runs ADD COLUMN deactivated TEXT;
CREATE INDEX IF NOT EXISTS runs_started ON runs (started_at);
CREATE INDEX IF NOT EXISTS posts_run ON posts (run_id);
"""

# The Latest page works per source
_SCHEMA_V8 = """
CREATE INDEX IF NOT EXISTS runs_source_name ON runs (source_kind, source_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS posts_found ON posts (found_at);
"""

# Downloads you ask for go ahead of bulk ones in the queue
_SCHEMA_V9 = """
ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN not_before TEXT;
CREATE INDEX IF NOT EXISTS jobs_order ON jobs (state, priority DESC, queued_at, id);
"""

# Index i holds the script that upgrades a database from version i to version i + 1
_MIGRATIONS = (
    _SCHEMA_V1,
    _SCHEMA_V2,
    _SCHEMA_V3,
    _SCHEMA_V4,
    _SCHEMA_V5,
    _SCHEMA_V6,
    _SCHEMA_V7,
    _SCHEMA_V8,
    _SCHEMA_V9,
)
SCHEMA_VERSION = len(_MIGRATIONS)


def default_db_path() -> Path:
    if env := os.environ.get("BDFRX_MANAGER_DB"):
        return Path(env).expanduser()
    return Path(appdirs.user_config_dir("bdfrx", "BDFRx"), "manager.db")


def connect(path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """Open the manager database, creating it and its schema if needed"""
    db_path = Path(path).expanduser() if path else default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    _enable_wal(conn)
    _migrate(conn)
    return conn


def _enable_wal(conn: sqlite3.Connection, timeout: float = 30) -> None:
    """Switch to WAL mode (readers don't block writers). The switch needs the DB to itself and SQLite doesn't wait
    for that, so when another connection is busy with it (e.g. several threads opening a new DB), retry briefly."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _migrate(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version;").fetchone()[0] >= SCHEMA_VERSION:
        return
    # Several connections can open an old DB at once (e.g. the server's threads at startup); the write lock makes the
    # others wait and then find the upgrade done, instead of all running it
    conn.execute("BEGIN IMMEDIATE;")
    try:
        version = conn.execute("PRAGMA user_version;").fetchone()[0]
        for target, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            for statement in script.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target};")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
