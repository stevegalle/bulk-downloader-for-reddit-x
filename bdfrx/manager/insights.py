"""Read-only views over what the manager has done: the Latest feed, where users post, run stats, things needing
attention; and tidying old run logs."""

import shlex
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from bdfrx.manager import candidates, runner

_SOURCE_STATUS = "(SELECT status FROM sources s WHERE s.kind = ? AND s.name = {column})"


def _ago(**delta: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def feed(
    conn: sqlite3.Connection,
    limit: int = 30,
    before: Optional[int] = None,
    kind: Optional[str] = None,
    name: Optional[str] = None,
) -> list[dict]:
    """Posts downloaded by runs, newest first, with what you could do next: follow or ignore their author (posts
    from subreddits), or follow or skip their subreddit (posts from users). `before` is a cursor from a previous
    page."""
    query = f"""
        SELECT p.rowid AS cursor, p.post_id, p.author, p.subreddit, p.score, p.posted_at, p.found_at,
               r.id AS run_id, r.source_kind, r.source_name,
               {_SOURCE_STATUS.format(column="p.author")} AS author_status,
               {_SOURCE_STATUS.format(column="p.subreddit")} AS subreddit_status,
               EXISTS (SELECT 1 FROM rules WHERE kind = 'ignore_user' AND value = p.author) AS author_ignored,
               EXISTS (SELECT 1 FROM rules WHERE kind = 'skip_subreddit' AND value = p.subreddit) AS subreddit_skipped
        FROM posts p JOIN runs r ON r.id = p.run_id
        WHERE 1 = 1
    """  # noqa: S608 - only fixed SQL is formatted in
    params: list = ["user", "subreddit"]
    if before is not None:
        query += " AND p.rowid < ?"
        params.append(before)
    if kind:
        query += " AND r.source_kind = ?"
        params.append(kind)
    if name:
        query += " AND r.source_name = ? COLLATE NOCASE"
        params.append(name)
    query += " ORDER BY p.rowid DESC LIMIT ?;"
    params.append(limit)
    posts = []
    for row in conn.execute(query, params).fetchall():
        post = candidates._post_with_files(conn, row)  # noqa: SLF001
        post["author_ignored"] = bool(post["author_ignored"])
        post["subreddit_skipped"] = bool(post["subreddit_skipped"])
        posts.append(post)
    return posts


def author_subreddits(conn: sqlite3.Connection, name: str) -> list[dict]:
    """Subreddits a user's recorded posts came from, most used first, and whether you follow or skip each.
    Their own profile subreddit (u_<name>) is left out."""
    rows = conn.execute(
        f"""
        SELECT p.subreddit AS name, COUNT(*) AS posts, MAX(p.posted_at) AS last_posted,
               {_SOURCE_STATUS.format(column="p.subreddit")} AS status,
               EXISTS (SELECT 1 FROM rules WHERE kind = 'skip_subreddit' AND value = p.subreddit) AS skipped
        FROM posts p
        WHERE p.author = ? AND p.subreddit NOT LIKE 'u\\_%' ESCAPE '\\'
        GROUP BY p.subreddit COLLATE NOCASE
        ORDER BY posts DESC, name COLLATE NOCASE;
        """,  # noqa: S608 - only fixed SQL is formatted in
        ("subreddit", name),
    ).fetchall()
    return [{**dict(row), "skipped": bool(row["skipped"])} for row in rows]


def _totals(conn: sqlite3.Connection, since: str) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS runs,
               SUM(status = 'ok') AS ok,
               SUM(status = 'failed') AS failed,
               COALESCE(SUM(files_new), 0) AS files_new,
               COALESCE(SUM(files_failed), 0) AS files_failed,
               COALESCE(SUM(files_retry), 0) AS files_retry,
               SUM(deactivated IS NOT NULL) AS deactivated
        FROM runs WHERE started_at >= ?;
        """,
        (since,),
    ).fetchone()
    return {key: row[key] or 0 for key in row.keys()}  # noqa: SIM118 - sqlite3.Row


def run_stats(conn: sqlite3.Connection, days: int = 30) -> dict:
    """Totals for the last day and week, and runs and new files per day (UTC) for the chart"""
    per_day = {
        row["day"]: dict(row)
        for row in conn.execute(
            """
            SELECT date(started_at) AS day, COUNT(*) AS runs, SUM(status = 'failed') AS failed,
                   COALESCE(SUM(files_new), 0) AS files_new
            FROM runs WHERE started_at >= ? GROUP BY day;
            """,
            (_ago(days=days),),
        )
    }
    today = datetime.now(timezone.utc).date()
    series = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        series.append(per_day.get(day, {"day": day, "runs": 0, "failed": 0, "files_new": 0}))
    return {"last_24h": _totals(conn, _ago(hours=24)), "last_7d": _totals(conn, _ago(days=7)), "per_day": series}


def attention(conn: sqlite3.Connection) -> dict:
    """Things worth a look: sources failing repeatedly, failed and interrupted runs, files that couldn't be fetched,
    and accounts deactivated in the last day"""
    since = _ago(hours=24)
    failing = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sources WHERE status = 'active' AND consecutive_failures >= 3 "
            "ORDER BY consecutive_failures DESC, name COLLATE NOCASE;",
        )
    ]
    failed_runs = [
        {**dict(row), "interrupted": "interrupted" in (row["output_tail"] or "")}
        for row in conn.execute(
            "SELECT * FROM runs WHERE status = 'failed' AND started_at >= ? ORDER BY id DESC LIMIT 20;",
            (since,),
        )
    ]
    file_problems = [
        dict(row)
        for row in conn.execute(
            """
            SELECT source_kind, source_name, SUM(COALESCE(files_failed, 0)) AS files_failed,
                   SUM(COALESCE(files_retry, 0)) AS files_retry, MAX(id) AS last_run_id
            FROM runs WHERE started_at >= ? AND (files_failed > 0 OR files_retry > 0)
            GROUP BY source_kind, source_name ORDER BY files_failed + files_retry DESC LIMIT 20;
            """,
            (since,),
        )
    ]
    deactivated = [
        dict(row)
        for row in conn.execute(
            "SELECT id, source_kind, source_name, deactivated, started_at FROM runs "
            "WHERE deactivated IS NOT NULL AND started_at >= ? ORDER BY id DESC;",
            (since,),
        )
    ]
    return {
        "failing_sources": failing,
        "failed_runs": failed_runs,
        "file_problems": file_problems,
        "deactivated": deactivated,
    }


def run_time_window(run: dict) -> Optional[str]:
    """The --time a run used, to run it again the same way"""
    try:
        args = shlex.split(run["command"] or "")
        return args[args.index("--time") + 1]
    except (ValueError, IndexError):
        return None


def prune_logs(settings: dict[str, str], now: Optional[float] = None) -> int:
    """Delete run log files older than log_retention_days; returns how many. Runs keep their last lines in the DB."""
    cutoff = (now or time.time()) - int(settings["log_retention_days"]) * 86400
    removed = 0
    directory = runner.log_dir(settings) / "runs"
    if not directory.is_dir():
        return 0
    for path in directory.glob("*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def log_usage(settings: dict[str, str]) -> dict:
    directory = runner.log_dir(settings) / "runs"
    files = list(directory.glob("*.log")) if directory.is_dir() else []
    return {"files": len(files), "bytes": sum(p.stat().st_size for p in files if p.exists())}
