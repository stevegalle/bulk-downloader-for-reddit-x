"""The API server's download queue and schedule.

The queue lives in the manager DB (the jobs table), so it survives restarts: waiting downloads stay queued, and a
download that was running when the server stopped is queued again at startup and resumes. bdfrx's history DB means
the resumed download skips what it already fetched. Worker threads take jobs in order; how many run at once is
limited by the download slots (max_concurrent_downloads), which also count downloads started outside the server.
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

from bdfrx.manager import db, insights, runner, service

logger = logging.getLogger(__name__)

RECENT = 10
IDLE_POLL = 5

# Downloads you asked for (following someone, adding a source, Download now, Run again) go ahead of bulk ones
# (Run all, the schedule); within a priority it's first come, first served
PRIORITY = {"follow": 1, "added": 1, "manual": 1}

# Time windows from narrowest to widest, for merging a request into a queued download of the same source
_TIME_ORDER = ("hour", "day", "week", "month", "year", "all")


def _wider_time(current: Optional[str], requested: Optional[str]) -> Optional[str]:
    """The wider of two time windows; None means the source's own setting, which an explicit request replaces"""
    if requested is None:
        return current
    if current is None:
        return requested
    return max(current, requested, key=_TIME_ORDER.index)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class DownloadQueue:
    """Downloads started through the API: Download now, following someone, adding with download, run all, the
    schedule. The worker checks each job's source is still there and active when it starts, so e.g. undoing a
    follow drops its queued download."""

    def __init__(self, db_path: Optional[Union[str, Path]], *, start: bool = True) -> None:
        self.db_path = db_path
        self.wake = threading.Condition()
        self.workers: list[threading.Thread] = []
        self.started = False
        self.stopping = False
        if start:
            self.start()

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    # Lifecycle

    def start(self) -> None:
        """Requeue downloads a previous server left running, then start the workers"""
        conn = self._conn()
        try:
            resumed = conn.execute(
                "UPDATE jobs SET state = 'pending', started_at = NULL, run_id = NULL, resumed = resumed + 1 "
                "WHERE state = 'running';",
            ).rowcount
            conn.commit()
        finally:
            conn.close()
        if resumed:
            logger.info(f"Queued {resumed} interrupted download(s) again")
        self.started = True
        self._ensure_workers()

    def stop(self) -> None:
        """Stop taking new jobs (running ones are resumed by the next server when this one's process ends)"""
        self.stopping = True
        with self.wake:
            self.wake.notify_all()

    # Adding, cancelling, looking

    def add(self, kind: str, name: str, time: Optional[str] = None, trigger: str = "manual") -> str:
        """Queue a download. Returns "queued", "moved up" (it was waiting behind bulk work and now goes first, with
        the wider of the two time windows), or "already queued" (already running, or already waiting at this
        priority)."""
        priority = PRIORITY.get(trigger, 0)
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            existing = conn.execute(
                "SELECT * FROM jobs WHERE state IN ('pending', 'running') AND kind = ? AND name = ? COLLATE NOCASE;",
                (kind, name),
            ).fetchone()
            if existing and existing["state"] == "pending" and priority > existing["priority"]:
                conn.execute(
                    # queued_at restarts, so among requested downloads it's first come, first served from now
                    "UPDATE jobs SET priority = ?, trigger = ?, time = ?, queued_at = ?, not_before = NULL "
                    "WHERE id = ?;",
                    (priority, trigger, _wider_time(existing["time"], time), _now(), existing["id"]),
                )
                conn.commit()
                return "moved up"
            if existing:
                conn.rollback()
                return "already queued"
            conn.execute(
                "INSERT INTO jobs (kind, name, time, trigger, queued_at, priority) VALUES (?, ?, ?, ?, ?, ?);",
                (kind, name, time, trigger, _now(), priority),
            )
            conn.commit()
        finally:
            conn.close()
        self._ensure_workers()
        with self.wake:
            self.wake.notify()
        return "queued"

    def cancel(self, job_id: Optional[int] = None) -> int:
        """Cancel one waiting job, or all of them; returns how many were cancelled"""
        conn = self._conn()
        try:
            query = "UPDATE jobs SET state = 'finished', status = 'cancelled', finished_at = ? WHERE state = 'pending'"
            params: list = [_now()]
            if job_id is not None:
                query += " AND id = ?"
                params.append(job_id)
            count = conn.execute(query + ";", params).rowcount
            conn.commit()
        finally:
            conn.close()
        return count

    def snapshot(self) -> dict:
        conn = self._conn()
        try:

            def rows(query: str, *params: object) -> list[dict]:
                return [dict(r) for r in conn.execute(query, params)]

            return {
                "running": rows("SELECT * FROM jobs WHERE state = 'running' ORDER BY started_at, id;"),
                "pending": rows("SELECT * FROM jobs WHERE state = 'pending' ORDER BY priority DESC, queued_at, id;"),
                "recent": rows(
                    "SELECT * FROM jobs WHERE state = 'finished' ORDER BY finished_at DESC, id DESC LIMIT ?;",
                    RECENT,
                ),
            }
        finally:
            conn.close()

    def busy(self) -> bool:
        conn = self._conn()
        try:
            return (
                conn.execute("SELECT 1 FROM jobs WHERE state IN ('pending', 'running') LIMIT 1;").fetchone() is not None
            )
        finally:
            conn.close()

    def queue_sources(self, conn: sqlite3.Connection, sources: list[dict], trigger: str) -> dict:
        """Queue sources, skipping any a rule excludes; returns counts"""
        rules: dict[str, list[str]] = {}
        for rule in service.list_rules(conn):
            rules.setdefault(rule["kind"], []).append(rule["value"])
        counts = {"queued": 0, "already_queued": 0, "skipped": 0}
        for source in sources:
            if runner.skip_reason(source, rules):
                counts["skipped"] += 1
            elif self.add(source["kind"], source["name"], trigger=trigger) in ("queued", "moved up"):
                counts["queued"] += 1
            else:
                counts["already_queued"] += 1
        return counts

    def _requeue_later(self, conn: sqlite3.Connection, job: dict) -> str:
        """Put a rate-limited download at the back of the queue, not to start before the backoff has passed"""
        minutes = int(service.get_settings(conn)["rate_limit_backoff_minutes"])
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO jobs (kind, name, time, trigger, queued_at, priority, not_before) "
            "VALUES (?, ?, ?, ?, ?, 0, ?);",
            (job["kind"], job["name"], job["time"], job["trigger"], _now(), retry_at),
        )
        conn.commit()
        with self.wake:
            self.wake.notify()
        return retry_at

    # Workers

    def _ensure_workers(self) -> None:
        """Enough workers to fill every slot; the slots themselves enforce the limit"""
        if not self.started or self.stopping:
            return
        conn = self._conn()
        try:
            wanted = int(service.get_settings(conn)["max_concurrent_downloads"])
        finally:
            conn.close()
        with self.wake:
            self.workers = [w for w in self.workers if w.is_alive()]
            while len(self.workers) < wanted:
                worker = threading.Thread(target=self._work, name=f"bdfrx-download-{len(self.workers)}", daemon=True)
                self.workers.append(worker)
                worker.start()

    def _claim(self, conn: sqlite3.Connection) -> Optional[dict]:
        """Take the next waiting job, unless the server already runs as many as max_concurrent_downloads allows
        (checked each time, so lowering the setting takes effect without a restart)"""
        limit = int(service.get_settings(conn)["max_concurrent_downloads"])
        conn.execute("BEGIN IMMEDIATE;")
        running = conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'running';").fetchone()[0]
        row = conn.execute(
            "SELECT * FROM jobs WHERE state = 'pending' AND (not_before IS NULL OR not_before <= ?) "
            "ORDER BY priority DESC, queued_at, id LIMIT 1;",
            (_now(),),
        ).fetchone()
        if row is None or running >= limit:
            conn.rollback()
            return None
        conn.execute("UPDATE jobs SET state = 'running', started_at = ? WHERE id = ?;", (_now(), row["id"]))
        conn.commit()
        return dict(row)

    def _work(self) -> None:
        conn = self._conn()
        try:
            while not self.stopping:
                job = self._claim(conn)
                if job is None:
                    with self.wake:
                        self.wake.wait(IDLE_POLL)
                    continue
                self._run(conn, job)
        finally:
            conn.close()

    def _run(self, conn: sqlite3.Connection, job: dict) -> None:
        def started(run_id: int) -> None:
            conn.execute("UPDATE jobs SET run_id = ? WHERE id = ?;", (run_id, job["id"]))
            conn.commit()

        outcome: dict = {"status": "failed", "files_new": None, "reason": None, "run_id": None}
        try:
            run = runner.run_one(
                conn,
                job["kind"],
                job["name"],
                time_override=job["time"],
                on_start=started,
                trigger=job["trigger"],
            )
            outcome.update(run_id=run["id"], status=run["status"], files_new=run["files_new"])
            if run["deactivated"]:
                outcome.update(status="deactivated", reason=run["deactivated"])
            if run["rate_limited"]:
                retry_at = self._requeue_later(conn, job)
                outcome.update(
                    status="rate-limited",
                    reason=f"rate limited by {run['rate_limited']}; queued again, not before {retry_at} UTC",
                )
        except service.ManagerError as e:  # unfollowed, paused, ignored, or already downloading
            outcome.update(status="skipped", reason=str(e))
        except Exception as e:
            logger.exception(f"Download of {job['kind']} {job['name']} failed")
            outcome.update(status="failed", reason=str(e))
        conn.execute(
            "UPDATE jobs SET state = 'finished', finished_at = ?, status = ?, files_new = ?, reason = ?, "
            "run_id = COALESCE(?, run_id) WHERE id = ?;",
            (_now(), outcome["status"], outcome["files_new"], outcome["reason"], outcome["run_id"], job["id"]),
        )
        conn.commit()


# Schedule


def run_schedule(queue_: DownloadQueue, db_path: Optional[Union[str, Path]], check_seconds: float = 30) -> None:
    """Queue every active source each run_every_hours (the last time is kept in the DB so restarts don't reset it),
    and delete old run logs once an hour"""
    last_prune = 0.0
    while not queue_.stopping:
        conn = db.connect(db_path)
        try:
            schedule_tick(queue_, conn)
            if time.monotonic() - last_prune > 3600:
                insights.prune_logs(service.get_settings(conn))
                last_prune = time.monotonic()
        except Exception:
            logger.exception("Scheduled run failed to queue")
        finally:
            conn.close()
        time.sleep(check_seconds)


def schedule_tick(queue_: DownloadQueue, conn: sqlite3.Connection) -> Optional[dict]:
    """Queue every active source if the schedule is due; returns the counts when it was"""
    due_at = schedule_due_at(conn)
    if due_at is None or due_at > datetime.now(timezone.utc):
        return None
    counts = queue_.queue_sources(conn, runner.select_sources(conn), trigger="schedule")
    service.set_state(conn, "last_scheduled_at", _now())
    return counts


# A scheduled time missed by more than this (e.g. the server was down) waits for the next one instead of catching up
SCHEDULE_GRACE = timedelta(hours=3)


def schedule_due_at(conn: sqlite3.Connection, now: Optional[datetime] = None) -> Optional[datetime]:
    """When the schedule next queues everything (UTC), or None when it is off.

    With run_at set (local "HH:MM"), it runs at that time and every run_every_hours after it, starting again from
    run_at each day, in the machine's local time (so daylight saving doesn't shift it). Without run_at, it runs
    run_every_hours after the last time.
    """
    settings = service.get_settings(conn)
    hours = float(settings["run_every_hours"])
    if hours <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    last_text = service.get_state(conn, "last_scheduled_at")
    last = datetime.strptime(last_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if last_text else None
    if not settings["run_at"]:
        return now if last is None else last + timedelta(hours=hours)
    slots = _schedule_slots(settings["run_at"], hours, now)
    # The first slot after the last scheduled run, or from now on if it never ran
    due = next(slot for slot in slots if (slot > last if last else slot >= now))
    if due < now - SCHEDULE_GRACE:
        due = next(slot for slot in slots if slot >= now)
    return due


def _schedule_slots(run_at: str, hours: float, around: datetime) -> list[datetime]:
    """Scheduled times (UTC) from the day before `around` to two days after: run_at local time each day, then every
    `hours` until the day ends"""
    anchor = datetime.strptime(run_at, "%H:%M").time()
    local_day = around.astimezone().date()
    slots = []
    for offset in range(-1, 3):
        day = local_day + timedelta(days=offset)
        start = datetime.combine(day, anchor)  # naive local wall-clock time
        step = 0
        while (wall := start + timedelta(hours=hours * step)).date() == day:
            slots.append(wall.astimezone().astimezone(timezone.utc))  # naive -> local, DST-aware -> UTC
            step += 1
    return sorted(slots)


def start_server_background(
    queue_: DownloadQueue,
    db_path: Optional[Union[str, Path]],
    *,
    schedule: bool,
) -> threading.Thread:
    """At server start: recover from any interrupted downloads first, then start the queue and the schedule, so a
    resumed download never races the recovery of its own leftover files"""

    def background() -> None:
        conn = db.connect(db_path)
        try:
            report = runner.recover(conn)
            if any(report.values()):
                logger.info(f"Recovered after an interruption: {report}")
            if removed := insights.prune_logs(service.get_settings(conn)):
                logger.info(f"Deleted {removed} run log(s) older than log_retention_days")
        except Exception:
            logger.exception("Recovery after an interruption failed")
        finally:
            conn.close()
        queue_.start()
        if schedule:
            run_schedule(queue_, db_path)

    thread = threading.Thread(target=background, name="bdfrx-startup", daemon=True)
    thread.start()
    return thread
