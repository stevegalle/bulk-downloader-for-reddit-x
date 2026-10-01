"""Browsing everything in one source's archive folder: files grouped into posts, newest first.

Order comes from the post IDs in file names: Reddit's IDs are base 36 and increase over time, so they sort posts by
when they were posted, which file dates can't (on a share, they're often when the file was copied). Files whose names
don't follow the scheme sort after the rest, by name. Listings are cached briefly so scrolling a big folder on a
network share doesn't re-read it for every page.
"""

import os
import threading
import time
from pathlib import Path
from typing import Optional

from bdfrx.manager import candidates

LISTING_TTL = 120

_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}


def _post_order(post_id: Optional[str]) -> int:
    try:
        return int(post_id, 36) if post_id else -1
    except ValueError:
        return -1


def source_posts(folder: Path, kind: str, name: str) -> list[dict]:
    """Every post in the folder, newest first: {post_id, author, subreddit, score, files: [file names]}"""
    key = f"{kind}:{folder}"
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < LISTING_TTL:
            return cached[1]
    try:
        names = sorted(entry.name for entry in os.scandir(folder) if entry.is_file() and not entry.name.startswith("."))
    except OSError:
        names = []
    posts: dict[str, dict] = {}
    for file_name in names:
        parsed = (
            candidates.parse_user_file_name(file_name, name)
            if kind == "user"
            else candidates.parse_file_name(file_name, name)
        )
        if parsed is None:
            # Not in the naming scheme: a post of its own, with nothing known about it
            posts[f"file:{file_name}"] = {
                "post_id": None,
                "author": None,
                "subreddit": None,
                "score": None,
                "files": [file_name],
            }
            continue
        post = posts.setdefault(
            parsed["post_id"],
            {
                "post_id": parsed["post_id"],
                "author": parsed["author"],
                "subreddit": parsed["subreddit"],
                "score": parsed["score"],
                "files": [],
            },
        )
        post["files"].append(file_name)
    ordered = sorted(posts.values(), key=lambda p: (_post_order(p["post_id"]), p["files"][0]), reverse=True)
    with _lock:
        _cache[key] = (now, ordered)
    return ordered


def forget(folder: Path, kind: str) -> None:
    with _lock:
        _cache.pop(f"{kind}:{folder}", None)


MEDIA_FILTERS = {
    "all": None,
    "images": {"image"},
    "videos": {"video"},
}


def gallery_page(
    folder: Path,
    kind: str,
    name: str,
    offset: int = 0,
    limit: int = 60,
    order: str = "newest",
    media: str = "all",
) -> dict:
    """A page of posts, each file with its kind; totals for the whole folder (after the media filter)"""
    wanted = MEDIA_FILTERS[media]
    posts = source_posts(folder, kind, name)
    if wanted:
        posts = [p for p in posts if candidates.file_kind(p["files"][0]) in wanted]
    if order == "oldest":
        posts = list(reversed(posts))
    page = [
        {
            **post,
            "files": [
                {"index": index, "name": file_name, "kind": candidates.file_kind(file_name)}
                for index, file_name in enumerate(post["files"])
            ],
        }
        for post in posts[offset : offset + limit]
    ]
    return {"total_posts": len(posts), "total_files": sum(len(p["files"]) for p in posts), "posts": page}


def file_in_folder(folder: Path, file_name: str) -> Optional[Path]:
    """The file if it is directly inside the folder, else None (never a path elsewhere)"""
    if not file_name or "/" in file_name or "\\" in file_name or file_name.startswith("."):
        return None
    path = folder / file_name
    try:
        if path.resolve().parent != folder.resolve() or not path.is_file():
            return None
    except OSError:
        return None
    return path
