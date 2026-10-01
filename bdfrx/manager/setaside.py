"""Moving an ignored author's files out of the archive for review, and putting them back.

Files are moved, never deleted or overwritten. Each file goes to <archive root>/<ignored_folder>/<author>/ under the
same subreddits/<name>/ or users/<name>/ path it had, where <archive root> is the folder holding its subreddits/ or
users/ folder. That keeps every move within one filesystem (a rename, instant even on a share), and makes restoring
exact. Every move is recorded in the set_aside table, and post_files follows the files so previews keep working.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TypedDict

from bdfrx.manager import candidates, gallery, runner, service

ARCHIVE_KINDS = ("subreddits", "users")


class Summary(TypedDict):
    moved: int
    skipped: list[str]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _archive_root(path: Path) -> Optional[Path]:
    """The archive root of a file laid out as <root>/(subreddits|users)/<name>/<file>, or None"""
    if len(path.parents) >= 3 and path.parents[1].name in ARCHIVE_KINDS:
        return path.parents[2]
    return None


def _aside_path(path: Path, root: Path, folder: str, author: str) -> Path:
    return root / folder / author / path.relative_to(root)


def planned_moves(conn: sqlite3.Connection, name: str) -> tuple[list[tuple[Optional[str], Path, Path]], list[str]]:
    """(post_id, from, to) for every file of the author still in the archive, and reasons for any left out"""
    settings = service.get_settings(conn)
    folder = settings["ignored_folder"]
    name = service.normalize_name("user", name)
    moves: list[tuple[Optional[str], Path, Path]] = []
    skipped: list[str] = []
    roots: set[Path] = {Path(settings["dest_root"]).expanduser()}

    rows = conn.execute(
        "SELECT f.post_id, f.path, p.author FROM post_files f JOIN posts p ON p.post_id = f.post_id "
        "WHERE p.author = ? ORDER BY f.path;",
        (name,),
    ).fetchall()
    author = rows[0]["author"] if rows else name
    for row in rows:
        path = Path(row["path"])
        root = _archive_root(path)
        if root is not None and root.parent.name == folder:
            continue  # already set aside
        if root is None:
            skipped.append(f"{path}: not in an archive subreddits/ or users/ folder")
            continue
        roots.add(root)
        moves.append((row["post_id"], path, _aside_path(path, root, folder, author)))

    # Their own folder, if they were ever a followed user; user runs don't record posts, so take the folder whole
    for root in sorted(roots):
        user_dir = root / "users" / author
        if user_dir.is_dir():
            moves.extend(
                (None, path, _aside_path(path, root, folder, author))
                for path in sorted(user_dir.rglob("*"))
                if path.is_file()
            )
    return moves, skipped


def set_aside(conn: sqlite3.Connection, name: str, *, dry_run: bool = False) -> Summary:
    """Move the author's files into the ignored folder. Missing files and occupied targets are skipped."""
    moves, skipped = planned_moves(conn, name)
    author = service.normalize_name("user", name)
    moved = 0
    for post_id, source, target in moves:
        if not source.exists():
            skipped.append(f"{source}: no longer there")
            continue
        if target.exists():
            skipped.append(f"{source}: {target} already exists")
            continue
        if dry_run:
            moved += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        runner.move_file(source, target)
        conn.execute(
            "INSERT INTO set_aside (author, post_id, from_path, to_path) VALUES (?, ?, ?, ?);",
            (author, post_id, str(source), str(target)),
        )
        if post_id is not None:
            conn.execute(
                "UPDATE post_files SET path = ? WHERE post_id = ? AND path = ?;",
                (str(target), post_id, str(source)),
            )
        conn.commit()  # per file, so an interruption never leaves the record behind the filesystem
        moved += 1
    if not dry_run:
        # Their own users/<name> folder is emptied by the move; subreddit folders are shared and stay
        for user_dir in {source.parent for post_id, source, _ in moves if post_id is None}:
            runner.remove_empty_dirs(user_dir)
    return {"moved": moved, "skipped": skipped}


def restore(conn: sqlite3.Connection, name: str) -> Summary:
    """Move the author's set-aside files back to where they were, unless something has taken their place"""
    author = service.normalize_name("user", name)
    rows = conn.execute(
        "SELECT * FROM set_aside WHERE author = ? AND restored_at IS NULL AND gone_at IS NULL ORDER BY id;",
        (author,),
    ).fetchall()
    if not rows:
        raise service.NotFoundError(f"No set-aside files for {author}")
    moved = 0
    skipped: list[str] = []
    for row in rows:
        source, target = Path(row["to_path"]), Path(row["from_path"])
        if not source.exists():
            # Deleted during review: stop tracking it as set aside
            conn.execute("UPDATE set_aside SET gone_at = ? WHERE id = ?;", (_now(), row["id"]))
            conn.commit()
            skipped.append(f"{source}: no longer there (deleted after review?)")
            continue
        if target.exists():
            skipped.append(f"{source}: {target} already exists")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        runner.move_file(source, target)
        conn.execute("UPDATE set_aside SET restored_at = ? WHERE id = ?;", (_now(), row["id"]))
        if row["post_id"] is not None:
            conn.execute(
                "UPDATE post_files SET path = ? WHERE post_id = ? AND path = ?;",
                (str(target), row["post_id"], str(source)),
            )
        conn.commit()
        moved += 1
    _remove_empty_dirs(conn, author)
    return {"moved": moved, "skipped": skipped}


def _remove_empty_dirs(conn: sqlite3.Connection, author: str) -> None:
    """Tidy up the author's folders in the ignored tree once restoring has emptied them"""
    folder = service.get_settings(conn)["ignored_folder"]
    tops = set()
    for row in conn.execute("SELECT to_path FROM set_aside WHERE author = ?;", (author,)):
        path = Path(row[0])
        for parent in path.parents:
            if parent.parent.name == folder:
                tops.add(parent)
                break
    for top in tops:
        if not top.is_dir():
            continue
        for directory in sorted((p for p in top.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if not any(directory.iterdir()):
                directory.rmdir()
        if not any(top.iterdir()):
            top.rmdir()


def file_count(conn: sqlite3.Connection, name: str) -> int:
    """How many of the author's files are still in the archive and would be moved"""
    moves, _ = planned_moves(conn, name)
    return len(moves)


def list_set_aside(conn: sqlite3.Connection) -> list[dict]:
    """Authors with files currently set aside"""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT author, COUNT(*) AS files, MIN(moved_at) AS first_moved, MAX(moved_at) AS last_moved "
            "FROM set_aside WHERE restored_at IS NULL AND gone_at IS NULL "
            "GROUP BY author COLLATE NOCASE ORDER BY author COLLATE NOCASE;",
        )
    ]


def _set_aside_rows(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM set_aside WHERE author = ? AND restored_at IS NULL AND gone_at IS NULL ORDER BY id;",
        (service.normalize_name("user", name),),
    ).fetchall()


def gallery_page(
    conn: sqlite3.Connection,
    name: str,
    offset: int = 0,
    limit: int = 60,
    order: str = "newest",
    media: str = "all",
) -> dict:
    """The author's set-aside files as gallery posts (same shape as a source's gallery), newest post first. Each file
    carries its set-aside record id, which is how it is fetched: only recorded files are ever served."""
    posts: dict[str, dict] = {}
    for row in _set_aside_rows(conn, name):
        path = Path(row["to_path"])
        # .../ignored/<author>/subreddits/<subreddit>/<file> or .../ignored/<author>/users/<author>/<file>
        subreddit = path.parent.name if path.parent.parent.name == "subreddits" else None
        key = row["post_id"] or f"file:{row['id']}"
        post = posts.setdefault(
            key,
            {"post_id": row["post_id"], "author": row["author"], "subreddit": subreddit, "score": None, "files": []},
        )
        post["files"].append({"id": row["id"], "name": path.name, "kind": candidates.file_kind(path.name)})
    wanted = gallery.MEDIA_FILTERS[media]
    ordered = sorted(
        posts.values(), key=lambda p: (gallery.post_order(p["post_id"]), p["files"][0]["name"]), reverse=True,
    )
    if wanted:
        ordered = [p for p in ordered if any(f["kind"] in wanted for f in p["files"])]
    if order == "oldest":
        ordered.reverse()
    page = [
        {
            **post,
            "files": [
                {**file, "index": index} for index, file in enumerate(sorted(post["files"], key=lambda f: f["name"]))
            ],
        }
        for post in ordered[offset : offset + limit]
    ]
    return {"total_posts": len(ordered), "total_files": sum(len(p["files"]) for p in ordered), "posts": page}


def set_aside_file(conn: sqlite3.Connection, name: str, record_id: int) -> Path:
    """The path of one of the author's set-aside files, by its record id"""
    row = conn.execute(
        "SELECT to_path FROM set_aside WHERE id = ? AND author = ? AND restored_at IS NULL AND gone_at IS NULL;",
        (record_id, service.normalize_name("user", name)),
    ).fetchone()
    if row is None or not Path(row[0]).is_file():
        raise service.NotFoundError(f"No set-aside file {record_id} for {name}")
    return Path(row[0])
