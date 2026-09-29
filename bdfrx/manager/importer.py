"""One-time import of the directory-per-source layout used by the old update.sh scripts"""

import sqlite3
from pathlib import Path

from bdfrx.manager import service

# data_local subdirectory -> (source kind, status)
LAYOUT = {
    "users": ("user", "active"),
    "subreddits": ("subreddit", "active"),
    "users_backup": ("user", "paused"),
    "banned": ("user", "banned"),
}


def import_directories(conn: sqlite3.Connection, root: Path, *, dry_run: bool = False) -> dict[str, list[str]]:
    """Create a source for every directory under root/<layout dir>/; existing sources are left alone.

    A name that appears in more than one layout directory takes the highest-ranked status: banned, then
    active, then paused. users_backup is mostly a snapshot of users, so being in users wins over it.
    """
    rank = {"paused": 0, "active": 1, "banned": 2}
    found: dict[tuple[str, str], tuple[str, str]] = {}
    invalid: list[str] = []
    for subdir, (kind, status) in LAYOUT.items():
        directory = root / subdir
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_dir():
                continue
            try:
                name = service.normalize_name(kind, entry.name)
            except service.ManagerError:
                invalid.append(f"{subdir}/{entry.name}")
                continue
            key = (kind, name.lower())
            if key not in found or rank[status] > rank[found[key][1]]:
                found[key] = (name, status)

    report: dict[str, list[str]] = {"added": [], "existing": [], "invalid": invalid}
    for (kind, _), (name, status) in sorted(found.items()):
        label = f"{kind} {name} ({status})"
        try:
            service.get_source(conn, kind, name)
            report["existing"].append(label)
            continue
        except service.NotFoundError:
            pass
        if not dry_run:
            service.add_source(conn, kind, name, status=status, note=f"imported from {root.name}")
        report["added"].append(label)
    return report
