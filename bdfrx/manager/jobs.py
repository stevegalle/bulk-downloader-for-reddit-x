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
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            existing = conn.execute(
                "SELECT 1 FROM jobs WHERE state IN ('pending', 'running') AND kind = ? AND name = ? COLLATE NOCASE;",
                (kind, name),
            ).fetchone()
            if existing:
                conn.rollback()
                return "already queued"
            conn.execute(
                "INSERT INTO jobs (kind, name, time, trigger, queued_at) VALUES (?, ?, ?, ?, ?);",
                (kind, name, time, trigger, _now()),
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
                "pending": rows("SELECT * FROM jobs WHERE state = 'pending' ORDER BY id;"),
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
            elif self.add(source["kind"], source["name"], trigger=trigger) == "queued":
                counts["queued"] += 1
            else:
                counts["already_queued"] += 1
        return counts

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
        row = conn.execute("SELECT * FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1;").fetchone()
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


def schedule_due_at(conn: sqlite3.Connection) -> Optional[datetime]:
    """When the schedule next queues everything, or None when it is off"""
    hours = float(service.get_settings(conn)["run_every_hours"])
    if hours <= 0:
        return None
    last = service.get_state(conn, "last_scheduled_at")
    if last is None:
        return datetime.now(timezone.utc)
    return datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(hours=hours)


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
