import os
import sqlite3
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

# Index i holds the script that upgrades a database from version i to version i + 1
_MIGRATIONS = (_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4)
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
    conn.execute("PRAGMA journal_mode = WAL;")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version;").fetchone()[0]
    for target, script in enumerate(_MIGRATIONS[version:], start=version + 1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {target};")
        conn.commit()
