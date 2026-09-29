"""Finds users worth following from the authors of posts downloaded in subreddit runs.

Authors are read from file names rather than asked of Reddit. bdfrx names files with the file scheme
{REDDITOR}_{SUBREDDIT}_{POSTID}_{UPVOTES}, older files lack the _{UPVOTES} part, and gallery posts add
_<n> per image. Since the subreddit is known from the folder, usernames containing '_' still split
correctly. bdfrx sets each file's modification time to the post's creation time, which gives posted_at.
"""

import os
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, TypedDict

from bdfrx.manager import service


class ParsedPost(TypedDict):
    post_id: str
    author: str
    subreddit: str
    score: Optional[int]
    posted_at: Optional[str]


def _name_pattern(subreddit: str) -> re.Pattern:
    return re.compile(
        rf"^(?P<author>[A-Za-z0-9_-]+?)_(?P<subreddit>{re.escape(subreddit)})_(?P<post_id>[a-z0-9]{{5,10}})"
        r"(?:_(?P<score>-?\d+))?(?:_\d+)?(?:\.\w+)?$",
        re.IGNORECASE,
    )


def parse_file_name(file_name: str, subreddit: str) -> Optional[ParsedPost]:
    """Author, post ID and score from a file downloaded from the given subreddit, or None if it doesn't fit"""
    match = _name_pattern(subreddit).match(file_name)
    if not match:
        return None
    return {
        "post_id": match.group("post_id").lower(),
        "author": match.group("author"),
        "subreddit": subreddit,
        "score": int(match.group("score")) if match.group("score") else None,
        "posted_at": None,
    }


def _posted_at(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return None


def parse_files(paths: Iterable[Path], subreddit: str) -> tuple[list[ParsedPost], int]:
    """Parse downloaded files into posts, one per post ID; returns (posts, number of unparseable files)"""
    posts: dict[str, ParsedPost] = {}
    unparsed = 0
    for path in paths:
        post = parse_file_name(path.name, subreddit)
        if post is None:
            unparsed += 1
            continue
        if post["post_id"] not in posts:
            post["posted_at"] = _posted_at(path)
            posts[post["post_id"]] = post
    return list(posts.values()), unparsed


def record_posts(conn: sqlite3.Connection, posts: Iterable[ParsedPost], run_id: Optional[int] = None) -> int:
    """Store posts not seen before; returns how many were new"""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO posts (post_id, author, subreddit, score, posted_at, run_id) "
        "VALUES (:post_id, :author, :subreddit, :score, :posted_at, :run_id);",
        [{**post, "run_id": run_id} for post in posts],
    )
    conn.commit()
    return conn.total_changes - before


def scan_archive(
    conn: sqlite3.Connection,
    root: Path,
    subreddits: Optional[list[str]] = None,
    progress: Callable[[str], None] = lambda _: None,
) -> dict[str, int]:
    """Record posts from files already in <root>/subreddits/<name>/, skipping posts already recorded.

    Every folder is scanned, whether or not it is a source, since old subreddits still show who posts there.
    """
    known = {row[0] for row in conn.execute("SELECT post_id FROM posts;")}
    wanted = {name.lower() for name in subreddits or []}
    totals = {"folders": 0, "files": 0, "new_posts": 0, "unparsed": 0}
    folders = sorted(
        (entry for entry in os.scandir(root / "subreddits") if entry.is_dir()),
        key=lambda e: e.name.lower(),
    )
    for index, folder in enumerate(folders, start=1):
        if wanted and folder.name.lower() not in wanted:
            continue
        pattern = _name_pattern(folder.name)
        new_paths, unparsed, files = [], 0, 0
        for entry in os.scandir(folder.path):
            if not entry.is_file():
                continue
            files += 1
            match = pattern.match(entry.name)
            if not match:
                unparsed += 1
            elif match.group("post_id").lower() not in known:
                new_paths.append(Path(entry.path))
        posts, _ = parse_files(new_paths, folder.name)
        new_posts = record_posts(conn, posts)
        known.update(post["post_id"] for post in posts)
        totals["folders"] += 1
        totals["files"] += files
        totals["new_posts"] += new_posts
        totals["unparsed"] += unparsed
        progress(f"[{index}/{len(folders)}] {folder.name}: {files} files, {new_posts} new posts, {unparsed} unparsed")
    return totals


def list_candidates(
    conn: sqlite3.Connection,
    min_posts: int = 1,
    since: Optional[str] = None,
    subreddit: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Authors seen in subreddit downloads who are not user sources, ignored, or dismissed; most active first"""
    query = """
        SELECT author AS name,
               COUNT(*) AS posts,
               COUNT(DISTINCT subreddit) AS subreddit_count,
               GROUP_CONCAT(DISTINCT subreddit) AS subreddits,
               SUM(score) AS total_score,
               MIN(posted_at) AS first_posted,
               MAX(posted_at) AS last_posted
        FROM posts
        WHERE author NOT IN (SELECT name FROM sources WHERE kind = 'user')
          AND author NOT IN (SELECT value FROM rules WHERE kind = 'ignore_user')
          AND author NOT IN (SELECT name FROM dismissed_authors)
          AND author NOT IN ('DELETED', '[deleted]')
    """
    params: list = []
    if subreddit:
        query += " AND author IN (SELECT author FROM posts WHERE subreddit = ?)"
        params.append(subreddit)
    query += " GROUP BY author COLLATE NOCASE HAVING COUNT(*) >= ?"
    params.append(min_posts)
    if since:
        query += " AND MAX(posted_at) >= ?"
        params.append(since)
    query += " ORDER BY posts DESC, total_score DESC, name COLLATE NOCASE"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    candidates = [dict(row) for row in conn.execute(query, params)]
    for candidate in candidates:
        candidate["subreddits"] = sorted(candidate["subreddits"].split(","), key=str.lower)
    return candidates


def dismiss(conn: sqlite3.Connection, name: str, reason: Optional[str] = None) -> None:
    """Hide an author from the candidate list without ignoring their posts"""
    name = service.normalize_name("user", name)
    conn.execute(
        "INSERT INTO dismissed_authors (name, reason) VALUES (?, ?) "
        "ON CONFLICT (name) DO UPDATE SET reason = COALESCE(excluded.reason, reason);",
        (name, reason),
    )
    conn.commit()


def undismiss(conn: sqlite3.Connection, name: str) -> None:
    name = service.normalize_name("user", name)
    if not conn.execute("DELETE FROM dismissed_authors WHERE name = ?;", (name,)).rowcount:
        raise service.NotFoundError(f"{name!r} is not dismissed")
    conn.commit()


def list_dismissed(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM dismissed_authors ORDER BY name COLLATE NOCASE;")]


def promote(conn: sqlite3.Connection, name: str) -> dict:
    """Make an author an active user source, adding them if needed"""
    name = service.normalize_name("user", name)
    conn.execute("DELETE FROM dismissed_authors WHERE name = ?;", (name,))
    try:
        source = service.get_source(conn, "user", name)
    except service.NotFoundError:
        # Keep the author's own capitalization from their posts, rather than however it was typed
        row = conn.execute("SELECT author FROM posts WHERE author = ? LIMIT 1;", (name,)).fetchone()
        return service.add_source(conn, "user", row[0] if row else name, note="promoted from candidates")
    if source["status"] != "active":
        return service.update_source(conn, "user", name, status="active")
    return source


def since_to_timestamp(since: str) -> str:
    """'30d', '12w', '6m' or a YYYY-MM-DD date -> 'YYYY-MM-DD HH:MM:SS' in UTC"""
    if match := re.fullmatch(r"(\d+)([dwm])", since.strip().lower()):
        days = int(match.group(1)) * {"d": 1, "w": 7, "m": 30}[match.group(2)]
        moment = datetime.now(timezone.utc).timestamp() - days * 86400
        return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        return datetime.strptime(since.strip(), "%Y-%m-%d").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise service.ManagerError(f"{since!r} is not a period like 30d, 12w, 6m or a date like 2026-01-31")
