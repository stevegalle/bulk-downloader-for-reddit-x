"""Turns sources, rules and settings into bdfrx invocations and records the results"""

import contextlib
import fcntl
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import appdirs

from bdfrx.manager import candidates, service

logger = logging.getLogger(__name__)

OUTPUT_TAIL_LINES = 40

# bdfrx log lines that mean the account itself is gone, not that this run hit a transient problem
_ACCOUNT_GONE = (
    (re.compile(r"User \S+ is banned"), "banned", "Reddit reports the account as suspended"),
    (re.compile(r"Could not find user \S+"), "banned", "Reddit could not find the account"),
)


class RunLockedError(service.ManagerError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _tail(output: str) -> str:
    return "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])


def log_dir(settings: dict[str, str]) -> Path:
    if settings["log_dir"]:
        return Path(settings["log_dir"]).expanduser()
    return Path(appdirs.user_config_dir("bdfrx", "BDFRx"), "manager")


def effective_options(settings: dict[str, str], source: dict) -> dict:
    """The source's own overrides, falling back to the global defaults"""
    return {
        "sort": source["sort"] or settings["default_sort"],
        "time": source["time"] or settings["default_time"],
        "filter_gender": source["filter_gender"] or settings["default_filter_gender"],
        "min_score": source["min_score"],
        "post_limit": source["post_limit"],
        "extra_args": source["extra_args"],
    }


def destination(settings: dict[str, str], source: dict) -> Path:
    return Path(settings["dest_root"]).expanduser() / f"{source['kind']}s" / source["name"]


def download_directory(settings: dict[str, str], source: dict) -> Path:
    if settings["staging_root"]:
        return Path(settings["staging_root"]).expanduser() / f"{source['kind']}s" / source["name"]
    return destination(settings, source)


# Rules that can grow long are handed to bdfrx as files rather than one argument per entry
RULE_FILES = {
    "exclude_post": ("exclude_ids.txt", "--exclude-id-file"),
    "ignore_user": ("ignore_users.txt", "--ignore-user-file"),
}


def rule_files(directory: Path, rules: dict[str, list[str]]) -> dict[str, Path]:
    """Where each non-empty file-based rule list goes for this run"""
    return {kind: directory / name for kind, (name, _) in RULE_FILES.items() if rules.get(kind)}


def write_rule_files(files: dict[str, Path], rules: dict[str, list[str]]) -> None:
    for kind, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rules[kind]) + "\n")


def build_command(  # noqa: PLR0912
    settings: dict[str, str],
    source: dict,
    rules: dict[str, list[str]],
    files: Optional[dict[str, Path]] = None,
    sort_override: Optional[str] = None,
) -> list[str]:
    """The bdfrx command for a source. Rules in files are passed by file; without a file, ignored users are
    passed one by one and excluded posts are left out."""
    files = files or {}
    opts = effective_options(settings, source)
    cmd = shlex.split(settings["bdfrx_command"]) if settings["bdfrx_command"] else [sys.executable, "-m", "bdfrx"]
    cmd += ["download", str(download_directory(settings, source))]

    if source["kind"] == "user":
        cmd += ["--user", source["name"], "--submitted"]
    else:
        cmd += ["--subreddit", source["name"]]

    cmd += ["--sort", sort_override or opts["sort"], "--time", opts["time"]]
    cmd += ["--file-scheme", settings["file_scheme"], "--folder-scheme", settings["folder_scheme"]]
    if settings["authenticate"].lower() in ("1", "true", "yes"):
        cmd.append("--authenticate")
    if settings["bdfrx_config"]:
        cmd += ["--config", str(Path(settings["bdfrx_config"]).expanduser())]

    cmd += ["--db", "--uredb"]
    if history_db := settings[f"history_db_{source['kind']}"]:
        cmd += ["--db-file", str(Path(history_db).expanduser())]

    if opts["filter_gender"] != "none":
        cmd += ["--filter-gender", opts["filter_gender"]]
    if opts["min_score"] is not None:
        cmd += ["--min-score", str(opts["min_score"])]
    if opts["post_limit"] is not None:
        cmd += ["--limit", str(opts["post_limit"])]

    if "ignore_user" not in files:
        for user in rules.get("ignore_user", []):
            cmd += ["--ignore-user", user]
    for subreddit in rules.get("skip_subreddit", []):
        cmd += ["--skip-subreddit", subreddit]
    for domain in rules.get("skip_domain", []):
        cmd += ["--skip-domain", domain]
    for kind, path in files.items():
        cmd += [RULE_FILES[kind][1], str(path)]

    if opts["extra_args"]:
        cmd += shlex.split(opts["extra_args"])
    return cmd


def skip_reason(source: dict, rules: dict[str, list[str]]) -> Optional[str]:
    """Why an active source should not be run, if a rule covers it"""
    name = source["name"].lower()
    if source["kind"] == "user" and name in {u.lower() for u in rules.get("ignore_user", [])}:
        return "user is ignored"
    if source["kind"] == "subreddit" and name in {s.lower() for s in rules.get("skip_subreddit", [])}:
        return "subreddit is skipped"
    return None


def _grouped_rules(conn: sqlite3.Connection) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for rule in service.list_rules(conn):
        grouped.setdefault(rule["kind"], []).append(rule["value"])
    return grouped


def _files_in(directory: Path) -> set[Path]:
    if not directory.exists():
        return set()
    return {path for path in directory.rglob("*") if path.is_file()}


def move_staged(staging: Path, dest: Path) -> tuple[int, int]:
    """Move downloaded files into the archive without overwriting; returns (moved, already_present)"""
    moved = skipped = 0
    for path in sorted(_files_in(staging)):
        target = dest / path.relative_to(staging)
        if target.exists():
            path.unlink()
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        move_file(path, target)
        moved += 1
    return moved, skipped


def move_file(source: Path, target: Path) -> None:
    """Move a file, keeping its modification time (the post date) where the destination allows it.

    shutil.move can't be used for this: across filesystems it copies the timestamps too, and fails outright on
    shares that don't let the user set them (e.g. a CIFS mount where files are owned by root).
    """
    try:
        source.rename(target)
        return
    except OSError:
        pass
    shutil.copyfile(source, target)
    stat = source.stat()
    with contextlib.suppress(OSError):
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    source.unlink()


@contextlib.contextmanager
def run_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "manager.lock").open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLockedError("Another manager run is already in progress")
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def is_locked(directory: Path) -> bool:
    try:
        with run_lock(directory):
            return False
    except RunLockedError:
        return True


def _parse_name_filter(name: str) -> tuple[Optional[str], str]:
    """'u/foo' -> ('user', 'foo'), 'r/foo' -> ('subreddit', 'foo'), 'foo' -> (None, 'foo')"""
    match = re.match(r"^/?(u|user|r)/(.+)$", name.strip(), re.IGNORECASE)
    if not match:
        return None, name.strip().strip("/").lower()
    kind = "subreddit" if match.group(1).lower() == "r" else "user"
    return kind, match.group(2).strip("/").lower()


def select_sources(
    conn: sqlite3.Connection,
    kinds: Optional[list[str]] = None,
    names: Optional[list[str]] = None,
) -> list[dict]:
    """Active sources, least recently run first. A name prefixed u/ or r/ matches only that kind."""
    wanted = [_parse_name_filter(name) for name in names or []]

    def matches(source: dict) -> bool:
        if kinds and source["kind"] not in kinds:
            return False
        name = source["name"].lower()
        return not wanted or any(n == name and k in (None, source["kind"]) for k, n in wanted)

    sources = [source for source in service.list_sources(conn, status="active") if matches(source)]
    return sorted(sources, key=lambda s: (s["last_run_at"] is not None, s["last_run_at"] or ""))


def run_source(
    conn: sqlite3.Connection,
    source: dict,
    settings: dict[str, str],
    rules: dict[str, list[str]],
    files: dict[str, Path],
    sort_override: Optional[str] = None,
) -> dict:
    cmd = build_command(settings, source, rules, files, sort_override)
    download_dir = download_directory(settings, source)
    download_dir.mkdir(parents=True, exist_ok=True)
    before = _files_in(download_dir)

    logs = log_dir(settings) / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    started = _now()
    log_path = logs / f"{started.replace(' ', '_').replace(':', '')}_{source['kind']}_{source['name']}.log"
    run_id = conn.execute(
        "INSERT INTO runs (source_id, source_kind, source_name, started_at, command, log_path) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (source["id"], source["kind"], source["name"], started, shlex.join(cmd), str(log_path)),
    ).lastrowid
    conn.commit()

    try:
        with log_path.open("w") as log_file:
            exit_code = subprocess.run(  # noqa: S603
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=int(settings["run_timeout"]),
                check=False,
            ).returncode
    except subprocess.TimeoutExpired:
        exit_code = -1
        with log_path.open("a") as log_file:
            log_file.write(f"\n[manager] bdfrx killed after {settings['run_timeout']} seconds\n")
    except OSError as e:
        exit_code = -1
        with log_path.open("a") as log_file:
            log_file.write(f"\n[manager] could not start bdfrx: {e}\n")

    output = log_path.read_text(errors="replace")
    downloaded = _files_in(download_dir) - before
    posts_found = None
    if source["kind"] == "subreddit":
        # Parse before any move below, while the files certainly exist, but record where they will end up
        final_dir = destination(settings, source)
        posts, _ = candidates.parse_files(
            downloaded,
            source["name"],
            final_path=lambda path: final_dir / path.relative_to(download_dir),
        )
        posts_found = candidates.record_posts(conn, posts, run_id)
    if settings["staging_root"]:
        files_new, files_skipped = move_staged(download_dir, destination(settings, source))
    else:
        files_new, files_skipped = len(downloaded), 0
    status = "ok" if exit_code == 0 else "failed"

    conn.execute(
        "UPDATE runs SET finished_at = ?, exit_code = ?, status = ?, files_new = ?, files_skipped = ?, "
        "posts_found = ?, output_tail = ? WHERE id = ?;",
        (_now(), exit_code, status, files_new, files_skipped, posts_found, _tail(output), run_id),
    )
    conn.execute(
        "UPDATE sources SET last_run_at = ?, last_status = ?, last_files = ?, "
        "consecutive_failures = CASE WHEN ? = 'ok' THEN 0 ELSE consecutive_failures + 1 END WHERE id = ?;",
        (started, status, files_new, status, source["id"]),
    )
    if source["kind"] == "user":
        for pattern, new_status, note in _ACCOUNT_GONE:
            if pattern.search(output):
                conn.execute(
                    "UPDATE sources SET status = ?, note = ? WHERE id = ?;",
                    (new_status, f"{note} ({started[:10]})", source["id"]),
                )
                break
    conn.commit()
    return service.get_run(conn, run_id)


def run(
    conn: sqlite3.Connection,
    kinds: Optional[list[str]] = None,
    names: Optional[list[str]] = None,
    sort_override: Optional[str] = None,
    *,
    dry_run: bool = False,
    progress: Callable[[str], None] = logger.info,
) -> list[dict]:
    """Run bdfrx for every selected active source; returns the run records (or planned commands when dry_run)"""
    settings = service.get_settings(conn)
    rules = _grouped_rules(conn)
    sources = select_sources(conn, kinds, names)
    directory = log_dir(settings)
    files = rule_files(directory, rules)

    if dry_run:
        planned = []
        for source in sources:
            reason = skip_reason(source, rules)
            cmd = None if reason else shlex.join(build_command(settings, source, rules, files, sort_override))
            planned.append({"kind": source["kind"], "name": source["name"], "skipped": reason, "command": cmd})
        return planned

    results = []
    with run_lock(directory):
        write_rule_files(files, rules)
        for index, source in enumerate(sources, start=1):
            if reason := skip_reason(source, rules):
                progress(f"[{index}/{len(sources)}] {source['kind']} {source['name']}: skipped, {reason}")
                continue
            progress(f"[{index}/{len(sources)}] {source['kind']} {source['name']}")
            result = run_source(conn, source, settings, rules, files, sort_override)
            found = f", {result['posts_found']} post(s) recorded" if result["posts_found"] is not None else ""
            progress(f"    {result['status']}, {result['files_new']} new file(s){found}")
            results.append(result)
    return results
