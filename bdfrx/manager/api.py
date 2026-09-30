"""HTTP API over the manager service. Requires the 'manager' extra (fastapi, uvicorn)."""

import secrets
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.security import APIKeyCookie, APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from bdfrx.manager import candidates, db, runner, service, setaside

SourceKind = Literal["user", "subreddit"]
SourceStatus = Literal["active", "paused", "banned"]
RuleKind = Literal["ignore_user", "exclude_post", "skip_subreddit", "skip_domain"]
Sort = Literal["hot", "top", "new", "controversial", "rising", "relevance"]
Time = Literal["all", "hour", "day", "week", "month", "year"]
Gender = Literal["male", "female", "none"]


class SourceFields(BaseModel):
    # extra_args is deliberately absent: it is passed to bdfrx's command line, so it is only editable locally
    model_config = ConfigDict(extra="forbid")

    status: Optional[SourceStatus] = None
    sort: Optional[Sort] = None
    time: Optional[Time] = None
    filter_gender: Optional[Gender] = None
    min_score: Optional[int] = None
    post_limit: Optional[int] = Field(default=None, ge=1)
    note: Optional[str] = None


class SourceCreate(SourceFields):
    kind: SourceKind
    name: str


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
UI_DIR = Path(__file__).parent / "ui"
# Browsers can't add headers to <img>/<video> requests, so the UI also puts the key in this cookie
KEY_COOKIE = "bdfrx_manager_key"


def create_app(  # noqa: PLR0915
    db_path: Optional[Union[str, Path]] = None,
    api_key: Optional[str] = None,
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
    run_thread: dict[str, Optional[threading.Thread]] = {"thread": None}

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]  # noqa: N806

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
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

    @app.get("/status")
    def get_status(conn: Conn) -> dict:
        thread = run_thread["thread"]
        return {**service.summary(conn), "run_in_progress": bool(thread and thread.is_alive())}

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
        fields = body.model_dump(exclude={"kind", "name"}, exclude_none=True)
        return service.add_source(conn, body.kind, body.name, **fields)

    @app.get("/sources/{kind}/{name}")
    def get_source(kind: SourceKind, name: str, conn: Conn) -> dict:
        source = service.get_source(conn, kind, name)
        settings = service.get_settings(conn)
        return {
            **source,
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
        """An author's most recent posts, newest first; each file has a url to fetch it"""
        posts = candidates.author_posts(conn, name, limit)
        for post in posts:
            for file in post["files"]:
                file["url"] = f"/posts/{post['post_id']}/files/{file['index']}"
        return posts

    @app.post("/candidates/{name}/promote")
    def promote_candidate(name: str, conn: Conn) -> dict:
        return candidates.promote(conn, name)

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

    @app.post("/set-aside/{name}")
    def do_set_aside(name: str, conn: Conn) -> dict:
        """Move the user's files into the archive's ignored folder. Nothing is deleted or overwritten."""
        return setaside.set_aside(conn, name)

    @app.delete("/set-aside/{name}")
    def restore_set_aside(name: str, conn: Conn) -> dict:
        """Move the user's set-aside files back where they were"""
        return setaside.restore(conn, name)

    # Post files

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
    def list_runs(
        conn: Conn,
        limit: int = Query(50, ge=1, le=1000),
        kind: Optional[SourceKind] = None,
        name: Optional[str] = None,
        status: Optional[Literal["running", "ok", "failed"]] = None,
    ) -> list[dict]:
        return service.list_runs(conn, limit, kind, name, status)

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
        """Start a run in the background (or return the planned commands for a dry run)"""
        if body.dry_run:
            return runner.run(conn, body.kinds, body.names, body.sort, dry_run=True)
        thread = run_thread["thread"]
        if (thread and thread.is_alive()) or runner.is_locked(runner.log_dir(service.get_settings(conn))):
            raise HTTPException(409, "A run is already in progress")

        def background() -> None:
            run_conn = db.connect(db_path)
            try:
                runner.run(run_conn, body.kinds, body.names, body.sort)
            except runner.RunLockedError:
                pass
            finally:
                run_conn.close()

        run_thread["thread"] = threading.Thread(target=background, name="bdfrx-manager-run", daemon=True)
        run_thread["thread"].start()
        return {"started": True}

    return app
