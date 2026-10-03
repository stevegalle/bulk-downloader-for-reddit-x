"""HTTP API over the manager service. Requires the 'manager' extra (fastapi, uvicorn)."""

import contextlib
import logging
import secrets
import sqlite3
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

import anyio
import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.security import APIKeyCookie, APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from bdfrx.manager import candidates, db, gallery, insights, jobs, runner, service, setaside, thumbs
from bdfrx.manager.jobs import DownloadQueue, schedule_due_at

logger = logging.getLogger(__name__)

SourceKind = Literal["user", "subreddit"]
SourceStatus = Literal["active", "paused", "banned"]
RuleKind = Literal["ignore_user", "exclude_post", "skip_subreddit", "skip_domain", "exclude_title", "exclude_flair"]
Sort = Literal["hot", "top", "new", "controversial", "rising", "relevance"]
Time = Literal["all", "hour", "day", "week", "month", "year"]


class SourceFields(BaseModel):
    # extra_args is deliberately absent: it is passed to bdfrx's command line, so it is only editable locally
    model_config = ConfigDict(extra="forbid")

    status: Optional[SourceStatus] = None
    sort: Optional[Sort] = None
    time: Optional[Time] = None
    post_filters: Optional[bool] = Field(default=None, description="Whether the title and flair filter rules apply")
    min_score: Optional[int] = None
    post_limit: Optional[int] = Field(default=None, ge=1)
    note: Optional[str] = None


class SourceCreate(SourceFields):
    kind: SourceKind
    name: str
    download: bool = Field(False, description="Also download their posts now, over follow_download_time")


class RuleCreate(BaseModel):
    kind: RuleKind
    value: str
    reason: Optional[str] = None


class SettingValue(BaseModel):
    value: Optional[str] = Field(description="New value; null restores the default")


class Dismissal(BaseModel):
    reason: Optional[str] = None


class RunRequest(BaseModel):
    kinds: Optional[list[SourceKind]] = None
    names: Optional[list[str]] = None
    sort: Optional[Sort] = None
    dry_run: bool = False


PUBLIC_PATHS = frozenset(("/",))


class UIFiles(StaticFiles):
    """The web UI's files, which browsers must re-check on every load so an updated UI is picked up at once
    (an unchanged file costs a 304 Not Modified)"""

    def file_response(self, *args, **kwargs) -> Response:  # noqa: ANN002, ANN003
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


UI_DIR = Path(__file__).parent / "ui"
# Browsers can't add headers to <img>/<video> requests, so the UI also puts the key in this cookie
KEY_COOKIE = "bdfrx_manager_key"


def create_app(  # noqa: PLR0915
    db_path: Optional[Union[str, Path]] = None,
    api_key: Optional[str] = None,
    *,
    schedule: bool = True,
) -> FastAPI:
    """The API app. With api_key set, every endpoint requires it, in an X-API-Key header or the key cookie."""
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Required when the server has a key")
    key_cookie = APIKeyCookie(name=KEY_COOKIE, auto_error=False, description="Alternative to the header, for media")

    def check_key(
        request: Request,
        header: Annotated[Optional[str], Security(key_header)],
        cookie: Annotated[Optional[str], Security(key_cookie)],
    ) -> None:
        # Pages that only point at or ask for the key must load without it; everything returning data needs it
        if not api_key or request.url.path in PUBLIC_PATHS:
            return
        supplied = header or cookie
        if not (supplied and secrets.compare_digest(supplied.encode(), api_key.encode())):
            raise HTTPException(401, "Missing or wrong X-API-Key header")

    app = FastAPI(title="bdfrx manager", version="1", dependencies=[Depends(check_key)])
    # Recovery from any interrupted downloads runs first, then the (persistent) queue and the schedule start
    downloads = DownloadQueue(db_path, start=False)
    app.state.downloads = downloads
    app.state.startup = jobs.start_server_background(downloads, db_path, schedule=schedule)
    app.router.on_shutdown.append(downloads.stop)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]  # noqa: N806

    # Thumbnails wait for ffmpeg (two at a time) and read a network share. Waiting in the request's own worker thread
    # would let a gallery's worth of thumbnails occupy every worker and stall the rest of the API, so they run on a
    # few threads of their own and wait for those without holding a worker.
    thumbnail_threads = anyio.CapacityLimiter(4)

    async def in_thumbnail_thread(function: Callable, *args: object) -> object:
        return await anyio.to_thread.run_sync(function, *args, limiter=thumbnail_threads)

    @app.exception_handler(service.ManagerError)
    def manager_error(_request, error: service.ManagerError) -> Response:
        status = 400
        if isinstance(error, service.NotFoundError):
            status = 404
        elif isinstance(error, runner.RunLockedError):
            status = 409
        return PlainTextResponse(str(error), status_code=status)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    # The web UI is static files; it asks for the key itself, so it loads without one (mounts skip check_key)
    app.mount("/ui", UIFiles(directory=UI_DIR, html=True), name="ui")

    @app.get("/status")
    def get_status(conn: Conn) -> dict:
        due_at = schedule_due_at(conn)
        return {
            **service.summary(conn),
            "run_in_progress": downloads.busy(),
            "downloads": downloads.snapshot(),
            "schedule": {
                "every_hours": float(service.get_settings(conn)["run_every_hours"]),
                "run_at": service.get_settings(conn)["run_at"],
                "last_at": service.get_state(conn, "last_scheduled_at"),
                "next_at": due_at.strftime("%Y-%m-%d %H:%M:%S") if due_at else None,
            },
        }

    # Sources

    @app.get("/sources")
    def list_sources(
        conn: Conn,
        kind: Optional[SourceKind] = None,
        status: Optional[SourceStatus] = None,
        search: Optional[str] = None,
    ) -> list[dict]:
        return service.list_sources(conn, kind, status, search)

    @app.post("/sources", status_code=201)
    def add_source(body: SourceCreate, conn: Conn) -> dict:
        fields = body.model_dump(exclude={"kind", "name", "download"}, exclude_none=True)
        source = service.add_source(conn, body.kind, body.name, **fields)
        queued = None
        if body.download and source["status"] == "active":
            time_window = service.get_settings(conn)["follow_download_time"]
            queued = downloads.add(body.kind, source["name"], time_window, trigger="added")
        return {**source, "download": queued}

    @app.get("/sources/{kind}/{name}")
    def get_source(kind: SourceKind, name: str, conn: Conn) -> dict:
        source = service.get_source(conn, kind, name)
        settings = service.get_settings(conn)
        stats = next(s for s in service.list_sources(conn, kind, search=source["name"]) if s["id"] == source["id"])
        return {
            **stats,
            "effective_options": runner.effective_options(settings, source),
            "destination": str(runner.destination(settings, source)),
        }

    @app.patch("/sources/{kind}/{name}")
    def update_source(
        kind: SourceKind,
        name: str,
        body: SourceFields,
        conn: Conn,
    ) -> dict:
        """Fields present in the body are updated; an explicit null clears an override"""
        return service.update_source(conn, kind, name, **body.model_dump(exclude_unset=True))

    @app.post("/sources/{kind}/{name}/download", status_code=202)
    def download_source(kind: SourceKind, name: str, conn: Conn, time: Optional[Time] = None) -> dict:
        """Download one source now, in the background, alongside any scheduled run"""
        source = service.get_source(conn, kind, name)
        return {"download": downloads.add(kind, source["name"], time, trigger="manual")}

    @app.get("/sources/{kind}/{name}/thumbnail", response_class=FileResponse)
    async def source_thumbnail(kind: SourceKind, name: str, conn: Conn) -> FileResponse:
        """A small preview of a random image or video from the source's archive folder; different each time"""
        source = service.get_source(conn, kind, name)
        settings = service.get_settings(conn)
        found = await in_thumbnail_thread(
            thumbs.random_thumbnail,
            runner.destination(settings, source),
            runner.log_dir(settings) / "thumbs",
        )
        if found is None:
            raise HTTPException(
                404,
                "No images or videos to preview" if thumbs.ffmpeg_available() else "ffmpeg is missing",
            )
        return FileResponse(found, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # A source's gallery: everything in its archive folder

    def source_folder(conn: sqlite3.Connection, kind: str, name: str) -> tuple[dict, Path]:
        source = service.get_source(conn, kind, name)
        return source, runner.destination(service.get_settings(conn), source)

    @app.get("/sources/{kind}/{name}/gallery")
    def source_gallery(
        kind: SourceKind,
        name: str,
        conn: Conn,
        offset: int = Query(0, ge=0),
        limit: int = Query(60, ge=1, le=500),
        order: Literal["newest", "oldest"] = "newest",
        media: Literal["all", "images", "videos"] = "all",
    ) -> dict:
        """Posts in the source's archive folder, newest first (by post ID), each file with a url, and whether you
        follow or ignore its author and follow or skip its subreddit"""
        source, folder = source_folder(conn, kind, name)
        page = gallery.gallery_page(folder, kind, source["name"], offset, limit, order, media)
        insights.with_statuses(conn, page["posts"])
        base = f"/sources/{kind}/{urllib.parse.quote(source['name'])}/files"
        for post in page["posts"]:
            for file in post["files"]:
                file["url"] = f"{base}/{urllib.parse.quote(file['name'])}"
        return page

    @app.get("/sources/{kind}/{name}/files/{file_name}", response_class=FileResponse)
    def source_file(kind: SourceKind, name: str, file_name: str, conn: Conn) -> FileResponse:
        """A file from the source's archive folder (only that folder). Supports range requests."""
        _, folder = source_folder(conn, kind, name)
        path = gallery.file_in_folder(folder, file_name)
        if path is None:
            raise HTTPException(404, "No such file in this source's folder")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/sources/{kind}/{name}/files/{file_name}/thumbnail", response_class=FileResponse)
    async def source_file_thumbnail(kind: SourceKind, name: str, file_name: str, conn: Conn) -> FileResponse:
        _, folder = source_folder(conn, kind, name)
        path = await in_thumbnail_thread(gallery.file_in_folder, folder, file_name)
        if path is None:
            raise HTTPException(404, "No such file in this source's folder")
        try:
            found = await in_thumbnail_thread(
                thumbs.thumbnail,
                path,
                runner.log_dir(service.get_settings(conn)) / "thumbs",
            )
        except thumbs.ThumbnailError as e:
            raise HTTPException(404, str(e))
        return FileResponse(found, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    @app.get("/downloads")
    def list_downloads(conn: Conn) -> dict:
        """The download queue: what is running (with files so far), what is waiting, and recent results"""
        state = downloads.snapshot()
        settings = service.get_settings(conn)
        for job in state["running"]:
            with contextlib.suppress(service.ManagerError):
                folder = runner.download_directory(settings, service.get_source(conn, job["kind"], job["name"]))
                job["files_so_far"] = (
                    sum(1 for p in folder.rglob("*") if p.is_file()) if settings["staging_root"] else None
                )
        state["slots"] = int(settings["max_concurrent_downloads"])
        return state

    @app.delete("/downloads/{job_id}")
    def cancel_download(job_id: int) -> dict:
        """Cancel a waiting download (one already running finishes)"""
        if not downloads.cancel(job_id):
            raise HTTPException(404, "No waiting download with that id (it may have started already)")
        return {"cancelled": 1}

    @app.delete("/downloads")
    def cancel_all_downloads() -> dict:
        """Cancel every waiting download"""
        return {"cancelled": downloads.cancel()}

    @app.delete("/sources/{kind}/{name}", status_code=204)
    def remove_source(kind: SourceKind, name: str, conn: Conn) -> None:
        service.remove_source(conn, kind, name)

    # Rules

    @app.get("/rules")
    def list_rules(conn: Conn, kind: Optional[RuleKind] = None) -> list[dict]:
        return service.list_rules(conn, kind)

    @app.post("/rules", status_code=201)
    def add_rule(body: RuleCreate, conn: Conn) -> dict:
        if body.kind == "ignore_user":
            return service.ignore_user(conn, body.value, body.reason)
        return service.add_rule(conn, body.kind, body.value, body.reason)

    @app.delete("/rules/{kind}/{value:path}", status_code=204)
    def remove_rule(kind: RuleKind, value: str, conn: Conn) -> None:
        service.remove_rule(conn, kind, value)

    # Candidates

    @app.get("/candidates")
    def list_candidates(
        conn: Conn,
        min_posts: int = Query(1, ge=1),
        since: Optional[str] = Query(None, description="30d, 12w, 6m, or YYYY-MM-DD"),
        subreddit: Optional[str] = None,
        limit: int = Query(50, ge=0, le=5000, description="0 for all"),
        offset: int = Query(0, ge=0),
    ) -> list[dict]:
        since_ts = candidates.since_to_timestamp(since) if since else None
        return candidates.list_candidates(conn, min_posts, since_ts, subreddit, limit or None, offset)

    @app.get("/candidates/{name}/posts")
    def candidate_posts(name: str, conn: Conn, limit: int = Query(12, ge=1, le=200)) -> list[dict]:
        """An author's most recent posts, newest first; each file has a url to fetch it. Like the feed, each says
        whether you follow or ignore its author and follow or skip its subreddit."""
        posts = insights.with_statuses(conn, candidates.author_posts(conn, name, limit))
        for post in posts:
            for file in post["files"]:
                file["url"] = f"/posts/{post['post_id']}/files/{file['index']}"
        return posts

    @app.post("/candidates/{name}/promote")
    def promote_candidate(
        name: str,
        conn: Conn,
        download: bool = Query(True, description="Also download their posts now (time window: follow_download_time)"),
    ) -> dict:
        source = candidates.promote(conn, name)
        queued = None
        if download:
            queued = downloads.add(
                "user",
                source["name"],
                service.get_settings(conn)["follow_download_time"],
                trigger="follow",
            )
        return {**source, "download": queued}

    @app.get("/candidates/dismissed")
    def list_dismissed(conn: Conn) -> list[dict]:
        return candidates.list_dismissed(conn)

    @app.put("/candidates/{name}/dismissed", status_code=204)
    def dismiss_candidate(name: str, conn: Conn, body: Optional[Dismissal] = None) -> None:
        candidates.dismiss(conn, name, body.reason if body else None)

    @app.delete("/candidates/{name}/dismissed", status_code=204)
    def undismiss_candidate(name: str, conn: Conn) -> None:
        candidates.undismiss(conn, name)

    # Set-aside files of ignored users

    @app.get("/set-aside")
    def list_set_aside(conn: Conn) -> list[dict]:
        """Users whose files are currently set aside"""
        return setaside.list_set_aside(conn)

    @app.get("/set-aside/{name}")
    def plan_set_aside(name: str, conn: Conn) -> dict:
        """How many of the user's files are in the archive and would be moved"""
        summary = setaside.set_aside(conn, name, dry_run=True)
        return {"files": summary["moved"], "skipped": summary["skipped"]}

    @app.get("/set-aside/{name}/gallery")
    def set_aside_gallery(
        name: str,
        conn: Conn,
        offset: int = Query(0, ge=0),
        limit: int = Query(60, ge=1, le=500),
        order: Literal["newest", "oldest"] = "newest",
        media: Literal["all", "images", "videos"] = "all",
    ) -> dict:
        """The user's set-aside files, as a gallery (same shape as a source's)"""
        page = setaside.gallery_page(conn, name, offset, limit, order, media)
        for post in page["posts"]:
            for file in post["files"]:
                file["url"] = f"/set-aside/{urllib.parse.quote(name)}/files/{file['id']}"
        return page

    @app.get("/set-aside/{name}/files/{record_id}", response_class=FileResponse)
    def set_aside_file(name: str, record_id: int, conn: Conn) -> FileResponse:
        return FileResponse(
            setaside.set_aside_file(conn, name, record_id), headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.get("/set-aside/{name}/files/{record_id}/thumbnail", response_class=FileResponse)
    async def set_aside_file_thumbnail(name: str, record_id: int, conn: Conn) -> FileResponse:
        path = setaside.set_aside_file(conn, name, record_id)
        try:
            found = await in_thumbnail_thread(
                thumbs.thumbnail, path, runner.log_dir(service.get_settings(conn)) / "thumbs",
            )
        except thumbs.ThumbnailError as e:
            raise HTTPException(404, str(e))
        return FileResponse(found, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    @app.post("/set-aside/{name}")
    def do_set_aside(name: str, conn: Conn) -> dict:
        """Move the user's files into the archive's ignored folder. Nothing is deleted or overwritten."""
        return setaside.set_aside(conn, name)

    @app.delete("/set-aside/{name}")
    def restore_set_aside(name: str, conn: Conn) -> dict:
        """Move the user's set-aside files back where they were"""
        return setaside.restore(conn, name)

    # Post files

    @app.get("/posts/{post_id}/files/{index}/thumbnail", response_class=FileResponse)
    async def post_file_thumbnail(post_id: str, index: int, conn: Conn) -> FileResponse:
        """A small JPEG preview of a post's image or video file"""
        path = candidates.post_file_path(conn, post_id, index)
        if not await in_thumbnail_thread(path.is_file):
            raise HTTPException(404, f"File no longer at {path.name}")
        try:
            found = await in_thumbnail_thread(
                thumbs.thumbnail,
                path,
                runner.log_dir(service.get_settings(conn)) / "thumbs",
            )
        except thumbs.ThumbnailError as e:
            raise HTTPException(404, str(e))
        return FileResponse(found, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    @app.get("/posts/{post_id}/files/{index}", response_class=FileResponse)
    def post_file(post_id: str, index: int, conn: Conn) -> FileResponse:
        """A recorded file of a post. Supports range requests, so videos can seek."""
        path = candidates.post_file_path(conn, post_id, index)
        if not path.is_file():
            raise HTTPException(404, f"File no longer at {path.name} (moved, deleted, or the share is offline)")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    # Settings

    @app.get("/settings")
    def get_settings(conn: Conn) -> dict:
        settings = service.get_settings(conn)
        return {
            key: {
                "value": value,
                "default": service.DEFAULT_SETTINGS[key],
                "help": service.SETTING_HELP[key],
                "editable": key not in service.LOCAL_ONLY_SETTINGS,
            }
            for key, value in settings.items()
        }

    @app.put("/settings/{key}")
    def set_setting(key: str, body: SettingValue, conn: Conn) -> dict:
        if key in service.LOCAL_ONLY_SETTINGS:
            raise HTTPException(403, f"{key} can only be changed on the server: bdfrx-manager config set {key} ...")
        service.set_setting(conn, key, body.value)
        return {key: service.get_settings(conn)[key]}

    # Runs

    @app.get("/runs")
    def list_runs(  # noqa: PLR0913
        conn: Conn,
        limit: int = Query(50, ge=1, le=1000),
        kind: Optional[SourceKind] = None,
        name: Optional[str] = None,
        status: Optional[Literal["running", "ok", "failed"]] = None,
        trigger: Optional[str] = Query(None, description="schedule, follow, added, manual, run-all, cli, or unknown"),
        since: Optional[str] = Query(None, description="24h, 7d, 30d, or YYYY-MM-DD"),
        before_id: Optional[int] = Query(None, description="Page back: only runs older than this id"),
        search: Optional[str] = Query(None, description="Only sources whose name contains this"),
    ) -> list[dict]:
        since_ts = candidates.since_to_timestamp(since) if since else None
        return service.list_runs(conn, limit, kind, name, status, trigger, since_ts, before_id, search)

    @app.get("/runs/stats")
    def run_stats(conn: Conn, days: int = Query(30, ge=1, le=365)) -> dict:
        """Totals for the last day and week, runs and new files per day, and how much the run logs take"""
        return {**insights.run_stats(conn, days), "logs": insights.log_usage(service.get_settings(conn))}

    @app.get("/runs/attention")
    def run_attention(conn: Conn) -> dict:
        """Sources failing repeatedly, failed and interrupted runs, files that couldn't be fetched, deactivations"""
        return insights.attention(conn)

    @app.post("/runs/{run_id}/again", status_code=202)
    def run_again(run_id: int, conn: Conn) -> dict:
        """Queue the run's source again, with the same time window"""
        run = service.get_run(conn, run_id)
        source = service.get_source(conn, run["source_kind"], run["source_name"])
        time_window = insights.run_time_window(run)
        return {
            "download": downloads.add(source["kind"], source["name"], time_window, trigger="manual"),
            "time": time_window,
        }

    # Latest downloads

    @app.get("/feed")
    def latest(
        conn: Conn,
        limit: int = Query(30, ge=1, le=200),
        before: Optional[int] = Query(None, description="The cursor of the last post of the previous page"),
        kind: Optional[SourceKind] = None,
        name: Optional[str] = None,
    ) -> list[dict]:
        """Posts downloaded by runs, newest first, each file with a url"""
        posts = insights.feed(conn, limit, before, kind, name)
        for post in posts:
            for file in post["files"]:
                file["url"] = f"/posts/{post['post_id']}/files/{file['index']}"
        return posts

    def with_urls(posts: list[dict]) -> list[dict]:
        for post in posts:
            for file in post["files"]:
                file["url"] = f"/posts/{post['post_id']}/files/{file['index']}"
        return posts

    @app.get("/feed/sources")
    def latest_sources(
        conn: Conn,
        days: int = Query(7, ge=1, le=3650),
        kind: Optional[SourceKind] = None,
    ) -> list[dict]:
        """Sources that downloaded posts in the last `days` days, most recent first, with counts"""
        return insights.feed_sources(conn, days, kind)

    @app.get("/feed/groups")
    def latest_groups(
        conn: Conn,
        days: int = Query(7, ge=1, le=3650),
        per_source: int = Query(12, ge=1, le=50),
        kind: Optional[SourceKind] = None,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict:
        """Recent downloads grouped by source: each source with its newest posts, paged by source"""
        result = insights.feed_groups(conn, days, per_source, kind, limit, offset)
        for group in result["groups"]:
            with_urls(group["items"])
        return result

    @app.get("/sources/user/{name}/subreddits")
    def user_subreddits(name: str, conn: Conn) -> list[dict]:
        """Subreddits a user posts in (from recorded posts), and whether you follow or skip each"""
        return insights.author_subreddits(conn, service.get_source(conn, "user", name)["name"])

    @app.get("/runs/{run_id}")
    def get_run(run_id: int, conn: Conn) -> dict:
        return service.get_run(conn, run_id)

    @app.get("/runs/{run_id}/log", response_class=PlainTextResponse)
    def get_run_log(run_id: int, conn: Conn) -> str:
        run = service.get_run(conn, run_id)
        log_path = Path(run["log_path"]) if run["log_path"] else None
        if log_path and log_path.exists():
            return log_path.read_text(errors="replace")
        return run["output_tail"] or ""

    @app.post("/runs", status_code=202)
    def start_run(body: RunRequest, conn: Conn) -> Union[dict, list[dict]]:
        """Queue every selected active source, least recently run first (or return the planned commands for a dry
        run). Queued downloads run max_concurrent_downloads at a time."""
        if body.dry_run:
            return runner.run(conn, body.kinds, body.names, body.sort, dry_run=True)
        return downloads.queue_sources(conn, runner.select_sources(conn, body.kinds, body.names), trigger="run-all")

    return app
