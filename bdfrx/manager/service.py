"""Operations on the manager database, shared by the CLI and the HTTP API"""

import re
import sqlite3
from typing import Any, Optional

SOURCE_KINDS = ("user", "subreddit")
SOURCE_STATUSES = ("active", "paused", "banned")
RULE_KINDS = ("ignore_user", "exclude_post", "skip_subreddit", "skip_domain")
SORTS = ("hot", "top", "new", "controversial", "rising", "relevance")
TIMES = ("all", "hour", "day", "week", "month", "year")
GENDERS = ("male", "female", "none")

# Per-source columns that override a global default when not NULL
SOURCE_OVERRIDES = ("sort", "time", "filter_gender", "min_score", "post_limit", "extra_args")
SOURCE_EDITABLE = ("status", "note", *SOURCE_OVERRIDES)

DEFAULT_SETTINGS = {
    "dest_root": "/mnt/c/bdfrx",
    "staging_root": "",
    "bdfrx_command": "",
    "bdfrx_config": "",
    "authenticate": "true",
    "file_scheme": "{REDDITOR}_{SUBREDDIT}_{POSTID}_{UPVOTES}",
    "folder_scheme": ".",
    "default_sort": "new",
    "default_time": "day",
    "default_filter_gender": "male",
    "history_db_user": "~/.config/bdfrx/userdbs/{name}.db",
    "history_db_subreddit": "",
    "log_dir": "",
    "run_timeout": "3600",
    "ignored_folder": "ignored",
    "follow_download_time": "all",
    "max_concurrent_downloads": "2",
    "run_every_hours": "0",
    "log_retention_days": "2",
    "rate_limit_backoff_minutes": "30",
}

SETTING_HELP = {
    "dest_root": "Archive root; files land in <dest_root>/users/<name> and <dest_root>/subreddits/<name>",
    "staging_root": "If set, download here first and move into dest_root afterwards (use when dest_root is remote)",
    "bdfrx_command": "Command used to invoke bdfrx; empty means '<this python> -m bdfrx'",
    "bdfrx_config": "Path passed to bdfrx --config; empty uses bdfrx's default",
    "authenticate": "Pass --authenticate to bdfrx (true/false)",
    "file_scheme": "bdfrx --file-scheme",
    "folder_scheme": "bdfrx --folder-scheme",
    "default_sort": f"Sort used when a source has none: {', '.join(SORTS)}",
    "default_time": f"Time filter used when a source has none: {', '.join(TIMES)}",
    "default_filter_gender": f"Gender filter used when a source has none: {', '.join(GENDERS)}",
    "history_db_user": "bdfrx --db-file for user sources; {name} gives each its own DB; empty uses bdfrx's default",
    "history_db_subreddit": "bdfrx --db-file for subreddit sources; {name} gives each its own DB; empty: bdfrx default",
    "log_dir": "Where per-run logs and the lock file go; empty means <config dir>/manager",
    "run_timeout": "Seconds before a single bdfrx invocation is killed",
    "ignored_folder": "Folder, beside users/ and subreddits/ in the archive, that set-aside files are moved into",
    "follow_download_time": f"Time filter for the download started when you follow a candidate: {', '.join(TIMES)}",
    "max_concurrent_downloads": "How many bdfrx downloads may run at once, across the server and the CLI (1-8)",
    "run_every_hours": "The server queues every active source this often (e.g. 6, or 0.5); 0 turns the schedule off",
    "log_retention_days": "Days to keep each run's full log file; results and the last lines stay in the DB for good",
    "rate_limit_backoff_minutes": "A download stopped by a site's rate limit goes to the back of the queue, not to "
    "start again for this long",
}


# Settings that name programs or paths the manager runs or writes to. Changing them over the network would let
# anyone who can reach the API run arbitrary commands, so the API treats them as read-only.
LOCAL_ONLY_SETTINGS = (
    "dest_root",
    "staging_root",
    "bdfrx_command",
    "bdfrx_config",
    "history_db_user",
    "history_db_subreddit",
    "log_dir",
    "ignored_folder",
)


class ManagerError(ValueError):
    pass


class NotFoundError(ManagerError):
    pass


def _strip_prefix(kind: str, name: str) -> str:
    prefix = r"(?:u|user)/" if kind == "user" else r"r/"
    return re.sub(rf"^/?{prefix}", "", name.strip(), flags=re.IGNORECASE).strip("/")


def normalize_name(kind: str, name: str) -> str:
    name = _strip_prefix(kind, name)
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", name):
        raise ManagerError(f"{name!r} is not a valid {kind} name")
    return name


def normalize_rule_value(kind: str, value: str) -> str:
    value = value.strip()
    if kind in ("ignore_user", "skip_subreddit"):
        return normalize_name(kind.split("_")[1], value)
    if kind == "exclude_post":
        if (match := re.search(r"/comments/([a-z0-9]+)", value, re.IGNORECASE)) or (
            match := re.fullmatch(r"(?:https?://)?redd\.it/([a-z0-9]+)/?", value, re.IGNORECASE)
        ):
            value = match.group(1)
        if not re.fullmatch(r"[a-z0-9]{4,10}", value, re.IGNORECASE):
            raise ManagerError(f"{value!r} is not a valid post ID or link")
        return value.lower()
    if kind == "skip_domain":
        value = re.sub(r"^https?://", "", value.lower()).split("/")[0]
        if not value or "." not in value:
            raise ManagerError(f"{value!r} is not a valid domain")
        return value
    raise ManagerError(f"Unknown rule kind {kind!r}; expected one of {', '.join(RULE_KINDS)}")


def _check_choice(field: str, value: Any, choices: tuple) -> None:
    if value is not None and value not in choices:
        raise ManagerError(f"Invalid {field} {value!r}; expected one of {', '.join(choices)}")


def _validate_source_fields(fields: dict) -> None:
    unknown = set(fields) - set(SOURCE_EDITABLE)
    if unknown:
        raise ManagerError(f"Unknown source field(s): {', '.join(sorted(unknown))}")
    _check_choice("status", fields.get("status"), SOURCE_STATUSES)
    _check_choice("sort", fields.get("sort"), SORTS)
    _check_choice("time", fields.get("time"), TIMES)
    _check_choice("filter_gender", fields.get("filter_gender"), GENDERS)
    for field in ("min_score", "post_limit"):
        if fields.get(field) is not None and not isinstance(fields[field], int):
            raise ManagerError(f"{field} must be an integer")


# Settings


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    settings = dict(DEFAULT_SETTINGS)
    settings.update(
        {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM settings;")
            if row["key"] in settings
        },
    )
    return settings


def get_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM state WHERE key = ?;", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value;",
        (key, value),
    )
    conn.commit()


def set_setting(conn: sqlite3.Connection, key: str, value: Optional[str]) -> None:
    """Set a setting; None restores the default"""
    if key not in DEFAULT_SETTINGS:
        raise ManagerError(f"Unknown setting {key!r}; expected one of {', '.join(DEFAULT_SETTINGS)}")
    choices = {
        "default_sort": SORTS,
        "default_time": TIMES,
        "default_filter_gender": GENDERS,
        "follow_download_time": TIMES,
    }
    if key in choices:
        _check_choice(key, value, choices[key])
    if key == "run_timeout" and value is not None and not value.isdigit():
        raise ManagerError("run_timeout must be a whole number of seconds")
    if key == "rate_limit_backoff_minutes" and value is not None and not (value.isdigit() and int(value) >= 1):
        raise ManagerError("rate_limit_backoff_minutes must be a whole number of minutes, at least 1")
    if key == "log_retention_days" and value is not None and not (value.isdigit() and int(value) >= 1):
        raise ManagerError("log_retention_days must be a whole number of days, at least 1")
    if key == "max_concurrent_downloads" and value is not None and not (value.isdigit() and 1 <= int(value) <= 8):
        raise ManagerError("max_concurrent_downloads must be a whole number from 1 to 8")
    if key == "run_every_hours" and value is not None:
        try:
            hours = float(value)
        except ValueError:
            hours = -1
        if hours < 0 or (0 < hours < 0.25):  # noqa: PLR2004
            raise ManagerError("run_every_hours must be 0 (off) or at least 0.25")
    if (
        key == "ignored_folder"
        and value is not None
        and (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value) or value in ("users", "subreddits"))
    ):
        raise ManagerError("ignored_folder must be a plain folder name other than users or subreddits")
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?;", (key,))
    else:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value;",
            (key, value),
        )
    conn.commit()


# Sources


def get_source(conn: sqlite3.Connection, kind: str, name: str) -> dict:
    _check_choice("kind", kind, SOURCE_KINDS)
    name = _strip_prefix(kind, name)
    row = conn.execute("SELECT * FROM sources WHERE kind = ? AND name = ?;", (kind, name)).fetchone()
    if not row:
        raise NotFoundError(f"No {kind} named {name!r}")
    return dict(row)


def list_sources(
    conn: sqlite3.Connection,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    _check_choice("kind", kind, SOURCE_KINDS)
    _check_choice("status", status, SOURCE_STATUSES)
    query = """
        SELECT s.*,
               COALESCE(r.total_runs, 0) AS total_runs,
               COALESCE(r.total_files, 0) AS total_files,
               r.last_ok_at
        FROM sources s
        LEFT JOIN (
            SELECT source_id,
                   COUNT(*) AS total_runs,
                   SUM(COALESCE(files_new, 0)) AS total_files,
                   MAX(CASE WHEN status = 'ok' THEN started_at END) AS last_ok_at
            FROM runs GROUP BY source_id
        ) r ON r.source_id = s.id
        WHERE 1 = 1
    """
    params: list = []
    if kind:
        query += " AND s.kind = ?"
        params.append(kind)
    if status:
        query += " AND s.status = ?"
        params.append(status)
    if search:
        query += " AND s.name LIKE ?"
        params.append(f"%{search}%")
    query += " ORDER BY s.kind, s.name COLLATE NOCASE;"
    return [dict(row) for row in conn.execute(query, params)]


def add_source(conn: sqlite3.Connection, kind: str, name: str, **fields: Any) -> dict:
    _check_choice("kind", kind, SOURCE_KINDS)
    name = normalize_name(kind, name)
    _validate_source_fields(fields)
    columns = ["kind", "name", *fields]
    try:
        conn.execute(
            f"INSERT INTO sources ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))});",  # noqa: S608
            (kind, name, *fields.values()),
        )
    except sqlite3.IntegrityError:
        raise ManagerError(f"{kind} {name!r} already exists")
    conn.commit()
    return get_source(conn, kind, name)


def update_source(conn: sqlite3.Connection, kind: str, name: str, **fields: Any) -> dict:
    """Update the given fields; a value of None clears an override so the global default applies"""
    source = get_source(conn, kind, name)
    _validate_source_fields(fields)
    if fields.get("status", "") is None:
        raise ManagerError("status cannot be cleared")
    if fields:
        assignments = ", ".join(f"{field} = ?" for field in fields)
        conn.execute(f"UPDATE sources SET {assignments} WHERE id = ?;", (*fields.values(), source["id"]))  # noqa: S608
        conn.commit()
    return get_source(conn, kind, source["name"])


def remove_source(conn: sqlite3.Connection, kind: str, name: str) -> None:
    source = get_source(conn, kind, name)
    conn.execute("DELETE FROM sources WHERE id = ?;", (source["id"],))
    conn.commit()


# Rules


def list_rules(conn: sqlite3.Connection, kind: Optional[str] = None) -> list[dict]:
    _check_choice("kind", kind, RULE_KINDS)
    if kind:
        rows = conn.execute("SELECT * FROM rules WHERE kind = ? ORDER BY value COLLATE NOCASE;", (kind,))
    else:
        rows = conn.execute("SELECT * FROM rules ORDER BY kind, value COLLATE NOCASE;")
    return [dict(row) for row in rows]


def add_rule(conn: sqlite3.Connection, kind: str, value: str, reason: Optional[str] = None) -> dict:
    value = normalize_rule_value(kind, value)
    conn.execute(
        "INSERT INTO rules (kind, value, reason) VALUES (?, ?, ?) "
        "ON CONFLICT (kind, value) DO UPDATE SET reason = COALESCE(excluded.reason, reason);",
        (kind, value, reason),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM rules WHERE kind = ? AND value = ?;", (kind, value)).fetchone())


def remove_rule(conn: sqlite3.Connection, kind: str, value: str) -> None:
    value = normalize_rule_value(kind, value)
    if not conn.execute("DELETE FROM rules WHERE kind = ? AND value = ?;", (kind, value)).rowcount:
        raise NotFoundError(f"No {kind} rule for {value!r}")
    conn.commit()


def ignore_user(conn: sqlite3.Connection, name: str, reason: Optional[str] = None) -> dict:
    """Ignore a user everywhere, and stop downloading them as a source if they are one"""
    rule = add_rule(conn, "ignore_user", name, reason)
    conn.execute(
        "UPDATE sources SET status = 'paused' WHERE kind = 'user' AND name = ? AND status = 'active';",
        (rule["value"],),
    )
    conn.commit()
    return rule


# Runs


def list_runs(  # noqa: PLR0913
    conn: sqlite3.Connection,
    limit: int = 50,
    kind: Optional[str] = None,
    name: Optional[str] = None,
    status: Optional[str] = None,
    trigger: Optional[str] = None,
    since: Optional[str] = None,
    before_id: Optional[int] = None,
    search: Optional[str] = None,
) -> list[dict]:
    """Runs, newest first. since is a 'YYYY-MM-DD HH:MM:SS' UTC timestamp; before_id pages back through history."""
    query, params = "SELECT * FROM runs WHERE 1 = 1", []
    if trigger:
        query += " AND COALESCE(trigger, 'unknown') = ?"
        params.append(trigger)
    if since:
        query += " AND started_at >= ?"
        params.append(since)
    if before_id:
        query += " AND id < ?"
        params.append(before_id)
    if search:
        query += " AND source_name LIKE ?"
        params.append(f"%{search}%")
    if kind:
        query += " AND source_kind = ?"
        params.append(kind)
    if name:
        query += " AND source_name = ? COLLATE NOCASE"
        params.append(name)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?;"
    params.append(limit)
    return [dict(row) for row in conn.execute(query, params)]


def get_run(conn: sqlite3.Connection, run_id: int) -> dict:
    row = conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
    if not row:
        raise NotFoundError(f"No run with id {run_id}")
    return dict(row)


def summary(conn: sqlite3.Connection) -> dict:
    counts = {
        f"{row['kind']}_{row['status']}": row["n"]
        for row in conn.execute("SELECT kind, status, COUNT(*) AS n FROM sources GROUP BY kind, status;")
    }
    rules = {row["kind"]: row["n"] for row in conn.execute("SELECT kind, COUNT(*) AS n FROM rules GROUP BY kind;")}
    last = conn.execute("SELECT MAX(started_at) FROM runs;").fetchone()[0]
    return {"sources": counts, "rules": rules, "last_run_at": last}
