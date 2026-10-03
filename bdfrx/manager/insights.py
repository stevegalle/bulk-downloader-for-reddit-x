"""Read-only views over what the manager has done: the Latest feed, where users post, run stats, things needing
attention; and tidying old run logs."""

import re
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


def with_statuses(conn: sqlite3.Connection, posts: list[dict]) -> list[dict]:
    """Adds what the feed has to each post that names them: whether you follow (and how) or ignore its author, and
    follow or skip its subreddit"""

    def lookup(query: str, names: set) -> dict:
        if not names:
            return {}
        marks = ", ".join("?" * len(names))
        return {row[0].lower(): row[1] for row in conn.execute(query.format(marks=marks), [*names])}

    authors = {p["author"] for p in posts if p.get("author")}
    subreddits = {p["subreddit"] for p in posts if p.get("subreddit")}
    status_query = "SELECT name, status FROM sources WHERE kind = '{kind}' AND name COLLATE NOCASE IN ({{marks}});"
    rule_query = "SELECT value, 1 FROM rules WHERE kind = '{kind}' AND value COLLATE NOCASE IN ({{marks}});"
    author_status = lookup(status_query.format(kind="user"), authors)
    subreddit_status = lookup(status_query.format(kind="subreddit"), subreddits)
    ignored = lookup(rule_query.format(kind="ignore_user"), authors)
    skipped = lookup(rule_query.format(kind="skip_subreddit"), subreddits)
    for post in posts:
        author = (post.get("author") or "").lower()
        subreddit = (post.get("subreddit") or "").lower()
        post["author_status"] = author_status.get(author)
        post["author_ignored"] = author in ignored
        post["subreddit_status"] = subreddit_status.get(subreddit)
        post["subreddit_skipped"] = subreddit in skipped
    return posts


def feed_sources(conn: sqlite3.Connection, days: int = 7, kind: Optional[str] = None) -> list[dict]:
    """Sources whose runs downloaded posts in the last `days` days, most recent first, with how many posts and the
    cursor of their newest one (for "unseen" markers)"""
    query = """
        SELECT r.source_kind AS kind, r.source_name AS name, COUNT(*) AS posts, MAX(p.found_at) AS last_found,
               MAX(p.rowid) AS latest_cursor,
               (SELECT status FROM sources s WHERE s.kind = r.source_kind AND s.name = r.source_name) AS status
        FROM posts p JOIN runs r ON r.id = p.run_id
        WHERE p.found_at >= ?
    """
    params: list = [_ago(days=days)]
    if kind:
        query += " AND r.source_kind = ?"
        params.append(kind)
    query += " GROUP BY r.source_kind, r.source_name COLLATE NOCASE ORDER BY last_found DESC, latest_cursor DESC;"
    return [dict(row) for row in conn.execute(query, params)]


def feed_groups(
    conn: sqlite3.Connection,
    days: int = 7,
    per_source: int = 12,
    kind: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """The feed grouped by source: each source with recent downloads and its newest posts. Paged by source."""
    sources = feed_sources(conn, days, kind)
    page = sources[offset : offset + limit]
    for source in page:
        source["items"] = feed(conn, per_source, kind=source["kind"], name=source["name"])
    return {"total": len(sources), "groups": page}


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
        {
            **dict(row),
            "interrupted": "interrupted" in (row["output_tail"] or ""),
            "rate_limited": bool(re.search(r"Stopped: rate limited by", row["output_tail"] or "")),
        }
        for row in conn.execute(
            # Leave out failures a later run of the same source has already made up for
            "SELECT * FROM runs f WHERE f.status = 'failed' AND f.started_at >= ? AND NOT EXISTS ("
            "    SELECT 1 FROM runs later WHERE later.source_kind = f.source_kind"
            "    AND later.source_name = f.source_name COLLATE NOCASE AND later.id > f.id AND later.status = 'ok'"
            ") ORDER BY f.id DESC LIMIT 20;",
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
