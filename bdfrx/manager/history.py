"""Splitting a shared bdfrx history DB into one DB per user.

A bdfrx history DB has three tables: post_id (posts already handled), link (URLs already handled) and hash (content
already downloaded, with the path it was saved to). None records whose post a row was, so rows are attributed by:

- hash rows: the saved path contains users/<name>/, and the file name carries the post ID
- post IDs of files in each archive's users/<name>/ folders
- posts the manager recorded from subreddit downloads, whose author is known

Rows that can't be attributed are left out: links (not needed, since bdfrx checks the post ID first), and posts that
failed and were marked done without producing a file (they get one more attempt). The shared DB is only read.
"""

import importlib.resources
import os
import re
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# The post ID in a file name after the author's name: <subreddit>_<postid>[_<score>][_<n>].<ext>, or older names
# without the score. IDs are lowercase base 36, which keeps them apart from mixed-case subreddit names.
_POST_ID = re.compile(r"(?:^|_)(?P<post_id>[a-z0-9]{5,10})(?:_-?\d+)?(?:_\d+)?\.[A-Za-z0-9]+$")


def post_id_from_file_name(file_name: str, author: str) -> Optional[str]:
    rest = file_name[len(author) + 1 :] if file_name.lower().startswith(f"{author.lower()}_") else file_name
    match = _POST_ID.search(rest)
    return match.group("post_id") if match else None


def _user_from_path(path: str) -> Optional[str]:
    parts = Path(path).parts
    for index in range(len(parts) - 3, -1, -1):
        if parts[index] == "users":
            return parts[index + 1]
    return None


@dataclass
class UserHistory:
    name: str
    post_ids: set[str] = field(default_factory=set)
    hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class SplitPlan:
    users: dict[str, UserHistory]  # keyed by lowercased name
    shared_post_ids: int
    attributed_post_ids: int


def plan_split(
    shared_db: Path,
    archive_roots: Iterable[Path] = (),
    known_names: Iterable[str] = (),
    progress: Callable[[str], None] = lambda _: None,
    manager_conn: Optional[sqlite3.Connection] = None,
) -> SplitPlan:
    """Work out each user's rows. known_names (e.g. user sources) supply preferred capitalization.

    With manager_conn, posts it recorded from subreddit downloads are attributed to their authors too, for users
    already found (a subreddit post by someone with no user history is not theirs to keep).
    """
    users: dict[str, UserHistory] = {}

    def user(name: str) -> UserHistory:
        return users.setdefault(name.lower(), UserHistory(name))

    for name in known_names:
        user(name)

    shared = sqlite3.connect(f"file:{shared_db}?mode=ro", uri=True)
    for digest, path in shared.execute("SELECT hash, path FROM hash WHERE path LIKE '%users%';"):
        name = _user_from_path(path)
        if not name:
            continue
        history = user(name)
        history.hashes[digest] = path
        if post_id := post_id_from_file_name(Path(path).name, name):
            history.post_ids.add(post_id)
    progress(f"{shared_db}: {sum(len(u.hashes) for u in users.values())} hash rows belong to users")

    for root in archive_roots:
        users_dir = Path(root) / "users"
        if not users_dir.is_dir():
            continue
        folders = sorted((e for e in os.scandir(users_dir) if e.is_dir()), key=lambda e: e.name.lower())
        for index, folder in enumerate(folders, start=1):
            history = user(folder.name)
            before = len(history.post_ids)
            for entry in os.scandir(folder.path):
                if entry.is_file() and (post_id := post_id_from_file_name(entry.name, folder.name)):
                    history.post_ids.add(post_id)
            progress(
                f"[{index}/{len(folders)}] {users_dir}/{folder.name}: {len(history.post_ids) - before} more post IDs",
            )

    shared_ids = {row[0] for row in shared.execute("SELECT post_id FROM post_id;")}
    shared.close()
    if manager_conn is not None:
        added = 0
        for post_id, author in manager_conn.execute("SELECT post_id, author FROM posts;"):
            history = users.get(author.lower())
            if history is not None and post_id in shared_ids and post_id not in history.post_ids:
                history.post_ids.add(post_id)
                added += 1
        progress(f"{added} more post IDs from posts recorded in subreddit downloads")
    attributed = set().union(*(u.post_ids for u in users.values())) if users else set()
    return SplitPlan(users, len(shared_ids), len(attributed & shared_ids))


def user_db_path(pattern: str, name: str) -> Path:
    return Path(pattern.replace("{name}", name)).expanduser()


def write_split(plan: SplitPlan, pattern: str, progress: Callable[[str], None] = lambda _: None) -> int:
    """Create or add to each user's DB; existing rows are kept, so running again is harmless. Returns DBs written."""
    written = 0
    template = importlib.resources.files("bdfrx") / "bdfrx.db"
    for history in sorted(plan.users.values(), key=lambda u: u.name.lower()):
        if not history.post_ids and not history.hashes:
            continue
        path = user_db_path(pattern, history.name)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with importlib.resources.as_file(template) as template_path:
                shutil.copy(template_path, path)
        conn = sqlite3.connect(path, timeout=60)
        conn.executemany("INSERT OR IGNORE INTO post_id (post_id) VALUES (?);", ((p,) for p in history.post_ids))
        conn.executemany("INSERT OR IGNORE INTO hash (hash, path) VALUES (?, ?);", history.hashes.items())
        conn.commit()
        conn.close()
        written += 1
        progress(f"{path}: {len(history.post_ids)} post IDs, {len(history.hashes)} hashes")
    return written
