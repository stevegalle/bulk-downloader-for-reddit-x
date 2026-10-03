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
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
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


# bdfrx log lines about files it couldn't get: for good (dead links, unsupported sites), or for now (retried next run)
_FILE_FAILED = re.compile(
    r"Failed to download resource|Could not download submission|failed to download submission"
    r"|Unexpected error downloading submission|Could not generate file path",
)
_FILE_RETRY = re.compile(r"will try again next run")
# bdfrx stops a run when a site keeps refusing with HTTP 429, and exits with this code
_RATE_LIMITED = re.compile(r"Stopped: rate limited by (\S+?);")
RATE_LIMITED_EXIT_CODE = 75


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
        "post_filters": bool(source["post_filters"]),
        "min_score": source["min_score"],
        "post_limit": source["post_limit"],
        "extra_args": source["extra_args"],
    }


def destination(settings: dict[str, str], source: dict) -> Path:
    return Path(settings["dest_root"]).expanduser() / f"{source['kind']}s" / source["name"]


def history_db_path(settings: dict[str, str], source: dict) -> Optional[Path]:
    """The source's bdfrx history DB; {name} in the setting becomes the source's name (one DB per source)"""
    pattern = settings[f"history_db_{source['kind']}"]
    if not pattern:
        return None
    return Path(pattern.replace("{name}", source["name"])).expanduser()


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
    """Write rule files atomically, since a run in another thread or process may be reading them"""
    for kind, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        temporary.write_text("\n".join(rules[kind]) + "\n")
        temporary.replace(path)


def default_bdfrx_command() -> list[str]:
    """The bdfrx installed alongside this Python.

    Its console script is preferred over 'python -m bdfrx', which puts the current directory first on the import
    path: started from a directory containing a folder named bdfrx (such as a venv at ~/bdfrx), it imports that
    folder instead of the package and fails.
    """
    script = Path(sys.executable).parent / "bdfrx"
    return [str(script)] if script.is_file() else [sys.executable, "-m", "bdfrx"]


_WINDOW_HOURS = {"hour": 1, "day": 24, "week": 7 * 24, "month": 31 * 24, "year": 366 * 24}


def since_for(
    conn: sqlite3.Connection,
    settings: dict[str, str],
    source: dict,
    time_override: Optional[str] = None,
) -> Optional[str]:
    """How far back a run should read, as a --since timestamp, or None to read everything Reddit lists.

    An explicit time window (e.g. "last week" from Download now) reads back that far, and "all" reads everything.
    Otherwise, with incremental_runs on, a run reads back to the start of the source's last successful run, less
    incremental_overlap_hours to catch posts that appear late; a source that never succeeded reads everything.
    """
    if time_override:
        hours = _WINDOW_HOURS.get(time_override)
        return _hours_ago(hours) if hours else None
    if settings["incremental_runs"].lower() != "true":
        return None
    last_ok = conn.execute(
        "SELECT MAX(started_at) FROM runs WHERE source_id = ? AND status = 'ok';",
        (source["id"],),
    ).fetchone()[0]
    if last_ok is None:
        return None
    start = datetime.strptime(last_ok, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (start - timedelta(hours=int(settings["incremental_overlap_hours"]))).strftime("%Y-%m-%d %H:%M:%S")


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def build_command(  # noqa: PLR0912
    settings: dict[str, str],
    source: dict,
    rules: dict[str, list[str]],
    files: Optional[dict[str, Path]] = None,
    sort_override: Optional[str] = None,
    time_override: Optional[str] = None,
    since: Optional[str] = None,
) -> list[str]:
    """The bdfrx command for a source. Rules in files are passed by file; without a file, ignored users are
    passed one by one and excluded posts are left out. since (see since_for) limits it to newer posts."""
    files = files or {}
    opts = effective_options(settings, source)
    cmd = shlex.split(settings["bdfrx_command"]) if settings["bdfrx_command"] else default_bdfrx_command()
    cmd += ["download", str(download_directory(settings, source))]

    if source["kind"] == "user":
        cmd += ["--user", source["name"], "--submitted"]
    else:
        cmd += ["--subreddit", source["name"]]

    cmd += ["--sort", sort_override or opts["sort"], "--time", time_override or opts["time"]]
    if since:
        cmd += ["--since", since]
    cmd += ["--file-scheme", settings["file_scheme"], "--folder-scheme", settings["folder_scheme"]]
    if settings["authenticate"].lower() in ("1", "true", "yes"):
        cmd.append("--authenticate")
    if settings["bdfrx_config"]:
        cmd += ["--config", str(Path(settings["bdfrx_config"]).expanduser())]

    cmd += ["--db", "--uredb"]
    if history_db := history_db_path(settings, source):
        cmd += ["--db-file", str(history_db)]

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
    if opts["post_filters"]:
        # As one argument, so a pattern starting with "-" isn't read as an option
        for pattern in rules.get("exclude_title", []):
            cmd.append(f"--exclude-title={pattern}")
        for pattern in rules.get("exclude_flair", []):
            cmd.append(f"--exclude-flair={pattern}")
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


def remove_empty_dirs(directory: Path) -> None:
    """Remove the directory and any directories inside it, if they hold no files. Never removes files."""
    if not directory.is_dir():
        return
    for path in sorted((p for p in directory.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):  # not empty after all, or already gone
            path.rmdir()
    with contextlib.suppress(OSError):
        directory.rmdir()


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
def run_lock(
    directory: Path,
    lock_name: str = "manager.lock",
    message: str = "Another manager run is already in progress",
) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / lock_name).open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLockedError(message)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def source_lock(directory: Path, source: dict) -> contextlib.AbstractContextManager[None]:
    """Held while one source downloads, so a scheduled run and an on-demand run never run the same source at once"""
    return run_lock(
        directory / "locks",
        f"{source['kind']}_{source['name'].lower()}.lock",
        f"{source['kind']} {source['name']} is already being downloaded",
    )


@contextlib.contextmanager
def download_slot(directory: Path, slots: int, poll_seconds: float = 2.0) -> Iterator[int]:
    """Hold one of `slots` download slots, waiting for one to free up.

    Slots are lock files, so the limit holds across every process running downloads: the API server's workers,
    'bdfrx-manager run' in a terminal, anything. The OS releases a slot if its holder dies.
    """
    directory = directory / "slots"
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        for index in range(slots):
            lock_file = (directory / f"slot-{index}.lock").open("w")
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                continue
            try:
                yield index
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()
            return
        time.sleep(poll_seconds)


def busy_slots(directory: Path, slots: int) -> int:
    """How many download slots are held right now"""
    busy = 0
    for index in range(slots):
        path = directory / "slots" / f"slot-{index}.lock"
        if not path.exists():
            continue
        with path.open("w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except BlockingIOError:
                busy += 1
    return busy


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
    time_override: Optional[str] = None,
    on_start: Optional[Callable[[int], None]] = None,
    trigger: str = "cli",
) -> dict:
    """Run bdfrx for one source, once one of the download slots is free"""
    slots = int(settings["max_concurrent_downloads"])
    with download_slot(log_dir(settings), slots):
        return _run_source(conn, source, settings, rules, files, sort_override, time_override, on_start, trigger)


def _run_source(  # noqa: PLR0913
    conn: sqlite3.Connection,
    source: dict,
    settings: dict[str, str],
    rules: dict[str, list[str]],
    files: dict[str, Path],
    sort_override: Optional[str],
    time_override: Optional[str],
    on_start: Optional[Callable[[int], None]],
    trigger: str,
) -> dict:
    cmd = build_command(
        settings,
        source,
        rules,
        files,
        sort_override,
        time_override,
        since_for(conn, settings, source, time_override),
    )
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
    if on_start is not None:
        on_start(run_id)

    try:
        with log_path.open("w") as log_file:
            exit_code = subprocess.run(  # noqa: S603
                cmd,
                # bdfrx looks in its current directory for config.cfg and bdfrx.db, so give every run the same one
                cwd=log_dir(settings),
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
    # Record the posts downloaded (author and subreddit, from the file names) for candidates and the Latest page.
    # Parse before any move below, while the files certainly exist, but record where they will end up.
    final_dir = destination(settings, source)
    posts, _ = candidates.parse_files(
        downloaded,
        source["name"] if source["kind"] == "subreddit" else None,
        final_path=lambda path: final_dir / path.relative_to(download_dir),
        author=source["name"] if source["kind"] == "user" else None,
    )
    if settings["staging_root"]:
        files_new, files_skipped = move_staged(download_dir, destination(settings, source))
    else:
        files_new, files_skipped = len(downloaded), 0
    # Recorded only now the files are in place, so nothing (e.g. the Latest page) asks for a file still in staging
    posts_found = candidates.record_posts(conn, posts, run_id)
    # A source that yielded nothing (no new posts, or a deleted account) shouldn't leave an empty folder behind
    remove_empty_dirs(download_dir)
    status = "ok" if exit_code == 0 else "failed"

    conn.execute(
        "UPDATE runs SET finished_at = ?, exit_code = ?, status = ?, files_new = ?, files_skipped = ?, "
        "posts_found = ?, output_tail = ?, trigger = ?, files_failed = ?, files_retry = ? WHERE id = ?;",
        (
            _now(),
            exit_code,
            status,
            files_new,
            files_skipped,
            posts_found,
            _tail(output),
            trigger,
            len(_FILE_FAILED.findall(output)),
            len(_FILE_RETRY.findall(output)),
            run_id,
        ),
    )
    conn.execute(
        "UPDATE sources SET last_run_at = ?, last_status = ?, last_files = ?, "
        "consecutive_failures = CASE WHEN ? = 'ok' THEN 0 ELSE consecutive_failures + 1 END WHERE id = ?;",
        (started, status, files_new, status, source["id"]),
    )
    # Deactivate accounts Reddit says are gone, so they aren't retried every run
    deactivated = None
    if source["kind"] == "user":
        for pattern, new_status, note in _ACCOUNT_GONE:
            if pattern.search(output):
                deactivated = f"{note} ({started[:10]})"
                conn.execute(
                    "UPDATE sources SET status = ?, note = ? WHERE id = ?;",
                    (new_status, deactivated, source["id"]),
                )
                conn.execute("UPDATE runs SET deactivated = ? WHERE id = ?;", (deactivated, run_id))
                break
    conn.commit()
    rate_limited = None
    if exit_code == RATE_LIMITED_EXIT_CODE and (match := _RATE_LIMITED.search(output)):
        rate_limited = match.group(1)
    return {**service.get_run(conn, run_id), "deactivated": deactivated, "rate_limited": rate_limited}


def run(
    conn: sqlite3.Connection,
    kinds: Optional[list[str]] = None,
    names: Optional[list[str]] = None,
    sort_override: Optional[str] = None,
    *,
    time_override: Optional[str] = None,
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
            cmd = (
                None
                if reason
                else shlex.join(
                    build_command(
                        settings,
                        source,
                        rules,
                        files,
                        sort_override,
                        time_override,
                        since_for(conn, settings, source, time_override),
                    ),
                )
            )
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
            try:
                with source_lock(directory, source):
                    result = run_source(conn, source, settings, rules, files, sort_override, time_override)
            except RunLockedError:
                progress("    skipped, already being downloaded by another run")
                continue
            found = f", {result['posts_found']} post(s) recorded" if result["posts_found"] is not None else ""
            progress(f"    {result['status']}, {result['files_new']} new file(s){found}")
            if result["deactivated"]:
                progress(f"    deactivated: {result['deactivated']}")
            if result["rate_limited"]:
                progress(f"    stopped early: rate limited by {result['rate_limited']}; the rest is tried next run")
            results.append(result)
    return results


def run_one(
    conn: sqlite3.Connection,
    kind: str,
    name: str,
    *,
    sort_override: Optional[str] = None,
    time_override: Optional[str] = None,
    on_start: Optional[Callable[[int], None]] = None,
    trigger: str = "cli",
) -> dict:
    """Download one active source now, alongside any batch run; only the same source running already blocks it"""
    settings = service.get_settings(conn)
    rules = _grouped_rules(conn)
    source = service.get_source(conn, kind, name)
    if source["status"] != "active":
        raise service.ManagerError(f"{kind} {source['name']} is {source['status']}, not active")
    if reason := skip_reason(source, rules):
        raise service.ManagerError(f"{kind} {source['name']} is not downloaded: {reason}")
    directory = log_dir(settings)
    files = rule_files(directory, rules)
    write_rule_files(files, rules)
    with source_lock(directory, source):
        return run_source(conn, source, settings, rules, files, sort_override, time_override, on_start, trigger)


def source_is_busy(directory: Path, source: dict) -> bool:
    """Whether some process is downloading this source right now (it holds the source's lock)"""
    try:
        with source_lock(directory, source):
            return False
    except RunLockedError:
        return True


def recover(conn: sqlite3.Connection, progress: Callable[[str], None] = logger.info) -> dict:
    """Tidy up after downloads that were cut off (the manager was stopped, crashed, or the machine lost power).

    For every source nothing is downloading now:
    - runs still marked running are marked failed, with a note in their log
    - files left in its staging folder are moved into the archive (bdfrx has already recorded those posts as done,
      so they would otherwise never arrive), recording subreddit posts as a normal run would
    Sources being downloaded by a live process (e.g. 'bdfrx-manager run' in a terminal) are left alone.
    """
    settings = service.get_settings(conn)
    directory = log_dir(settings)
    report = {"runs_closed": 0, "files_recovered": 0, "files_already_there": 0}
    sources = {s["id"]: s for s in service.list_sources(conn)}

    for run in conn.execute("SELECT * FROM runs WHERE status = 'running';").fetchall():
        source = sources.get(run["source_id"])
        if source is not None and source_is_busy(directory, source):
            continue
        note = "[manager] interrupted: the manager stopped while this ran; its download was queued again if queued"
        if run["log_path"] and Path(run["log_path"]).exists():
            with Path(run["log_path"]).open("a") as log_file:
                log_file.write(f"\n{note}\n")
        conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = ?, output_tail = COALESCE(output_tail, '') || ? "
            "WHERE id = ?;",
            (_now(), f"\n{note}", run["id"]),
        )
        conn.commit()
        report["runs_closed"] += 1
        progress(f"closed interrupted run {run['id']} ({run['source_kind']} {run['source_name']})")

    staging_root = settings["staging_root"]
    if staging_root and Path(staging_root).expanduser().is_dir():
        for source in sources.values():
            staged = download_directory(settings, source)
            if not staged.is_dir() or source_is_busy(directory, source):
                continue
            leftovers = _files_in(staged)
            if not leftovers:
                remove_empty_dirs(staged)
                continue
            with source_lock(directory, source):
                final_dir = destination(settings, source)
                posts, _ = candidates.parse_files(
                    leftovers,
                    source["name"] if source["kind"] == "subreddit" else None,
                    final_path=lambda path, final_dir=final_dir, staged=staged: final_dir / path.relative_to(staged),
                    author=source["name"] if source["kind"] == "user" else None,
                )
                # Credit the files to the source's last run (the interrupted one), so they show on the Latest page
                last_run = conn.execute(
                    "SELECT MAX(id) FROM runs WHERE source_id = ?;",
                    (source["id"],),
                ).fetchone()[0]
                candidates.record_posts(conn, posts, last_run)
                moved, skipped = move_staged(staged, destination(settings, source))
                remove_empty_dirs(staged)
            report["files_recovered"] += moved
            report["files_already_there"] += skipped
            progress(f"moved {moved} file(s) left in staging for {source['kind']} {source['name']} into the archive")
    return report
