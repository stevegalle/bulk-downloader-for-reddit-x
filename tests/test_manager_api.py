import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from bdfrx.manager import db, service
from bdfrx.manager.api import create_app
from tests.test_manager import fake_bdfrx  # noqa: F401


def _configure(db_path: Path, **settings: str) -> None:
    conn = db.connect(db_path)
    for key, value in settings.items():
        service.set_setting(conn, key, value)
    conn.close()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    _configure(tmp_path / "api.db", log_dir=str(tmp_path / "logs"), dest_root=str(tmp_path / "dest"))
    return TestClient(create_app(tmp_path / "api.db"))


def test_sources(client: TestClient):
    response = client.post("/sources", json={"kind": "user", "name": "u/alice", "sort": "top"})
    assert response.status_code == 201
    assert response.json()["name"] == "alice"
    assert client.post("/sources", json={"kind": "user", "name": "alice"}).status_code == 400
    assert client.post("/sources", json={"kind": "user", "name": "bob", "sort": "sideways"}).status_code == 422

    detail = client.get("/sources/user/alice").json()
    assert detail["effective_options"]["sort"] == "top"
    assert detail["destination"].endswith("users/alice")

    patched = client.patch("/sources/user/alice", json={"sort": None, "min_score": 7}).json()
    assert patched["sort"] is None
    assert patched["min_score"] == 7
    assert client.patch("/sources/user/alice", json={"status": None}).status_code == 400
    assert client.get("/sources/user/nobody").status_code == 404
    assert [s["name"] for s in client.get("/sources", params={"kind": "user"}).json()] == ["alice"]
    assert client.delete("/sources/user/alice").status_code == 204
    assert client.get("/sources").json() == []


def test_rules(client: TestClient):
    client.post("/sources", json={"kind": "user", "name": "alice"})
    assert client.post("/rules", json={"kind": "ignore_user", "value": "alice", "reason": "spam"}).status_code == 201
    assert client.get("/sources/user/alice").json()["status"] == "paused"
    post = client.post("/rules", json={"kind": "exclude_post", "value": "https://redd.it/ab12cd"}).json()
    assert post["value"] == "ab12cd"
    assert client.post("/rules", json={"kind": "skip_domain", "value": "nodot"}).status_code == 400
    assert len(client.get("/rules").json()) == 2
    assert client.delete("/rules/exclude_post/ab12cd").status_code == 204
    assert client.delete("/rules/exclude_post/ab12cd").status_code == 404


def test_settings(client: TestClient):
    settings = client.get("/settings").json()
    assert settings["default_sort"]["value"] == settings["default_sort"]["default"] == "new"
    assert settings["default_sort"]["editable"] is True
    assert client.put("/settings/default_sort", json={"value": "top"}).json() == {"default_sort": "top"}
    assert client.put("/settings/default_sort", json={"value": None}).json() == {"default_sort": "new"}
    assert client.put("/settings/nope", json={"value": "x"}).status_code == 400


def test_runs(client: TestClient, fake_bdfrx: Path, tmp_path: Path):  # noqa: F811
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {fake_bdfrx}")
    client.post("/sources", json={"kind": "user", "name": "alice"})
    plan = client.post("/runs", json={"dry_run": True}).json()
    assert plan[0]["name"] == "alice"

    assert client.post("/runs", json={}).status_code == 202
    for _ in range(100):
        if not client.get("/status").json()["run_in_progress"]:
            break
        time.sleep(0.1)
    runs = client.get("/runs").json()
    assert runs[0]["status"] == "ok"
    assert runs[0]["files_new"] == 2
    assert "ARGS" in client.get(f"/runs/{runs[0]['id']}/log").text
    assert client.get("/runs/999").status_code == 404


def test_candidates(client: TestClient, tmp_path: Path):
    folder = tmp_path / "dest" / "subreddits" / "pics"
    folder.mkdir(parents=True)
    for name in ("alice_pics_aaa111_10.jpg", "alice_pics_aaa222_3.jpg", "bob_pics_bbb111_1.jpg"):
        (folder / name).write_text("x")
    from bdfrx.manager import candidates

    conn = db.connect(tmp_path / "api.db")
    candidates.scan_archive(conn, tmp_path / "dest")
    conn.close()

    assert [c["name"] for c in client.get("/candidates").json()] == ["alice", "bob"]
    assert [c["name"] for c in client.get("/candidates", params={"min_posts": 2}).json()] == ["alice"]
    assert [c["name"] for c in client.get("/candidates", params={"limit": 1, "offset": 1}).json()] == ["bob"]
    assert client.get("/candidates", params={"since": "whenever"}).status_code == 400
    assert client.put("/candidates/bob/dismissed", json={"reason": "meh"}).status_code == 204
    assert client.get("/candidates/dismissed").json()[0]["reason"] == "meh"
    assert client.post("/candidates/alice/promote").json()["status"] == "active"
    assert client.get("/candidates").json() == []
    assert client.delete("/candidates/bob/dismissed").status_code == 204
    assert client.delete("/candidates/bob/dismissed").status_code == 404


def test_local_only_settings_are_read_only(client: TestClient):
    for key in service.LOCAL_ONLY_SETTINGS:
        response = client.put(f"/settings/{key}", json={"value": "/somewhere/else"})
        assert response.status_code == 403, key
        assert client.get("/settings").json()[key]["editable"] is False
    assert client.get("/settings").json()["default_sort"]["editable"] is True


def test_extra_args_not_writable(client: TestClient):
    assert client.post("/sources", json={"kind": "user", "name": "alice", "extra_args": "--log /x"}).status_code == 422
    client.post("/sources", json={"kind": "user", "name": "alice"})
    assert client.patch("/sources/user/alice", json={"extra_args": "--log /x"}).status_code == 422
    assert client.get("/sources/user/alice").json()["extra_args"] is None


def test_api_key(tmp_path: Path):
    keyed = TestClient(create_app(tmp_path / "keyed.db", api_key="s3cret"))
    assert keyed.get("/status").status_code == 401
    assert keyed.get("/status", headers={"X-API-Key": "wrong"}).status_code == 401
    assert keyed.get("/status", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert keyed.get("/docs").status_code == 200
    assert keyed.get("/", follow_redirects=False).headers["location"] == "/ui/"
    assert keyed.get("/ui/").status_code == 200
    assert keyed.get("/ui/app.js").headers["cache-control"] == "no-cache"
    open_app = TestClient(create_app(tmp_path / "open.db"))
    assert open_app.get("/status").status_code == 200
    assert open_app.get("/", follow_redirects=False).headers["location"] == "/ui/"


def _media_setup(tmp_path: Path) -> Path:
    folder = tmp_path / "dest" / "subreddits" / "pics"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "alice_pics_aaa111_10.jpg").write_bytes(b"0123456789" * 100)
    (folder / "alice_pics_aaa111_10_2.jpg").write_bytes(b"second")
    from bdfrx.manager import candidates

    conn = db.connect(tmp_path / "api.db")
    candidates.scan_archive(conn, tmp_path / "dest")
    conn.close()
    return folder


def test_candidate_posts_and_files(client: TestClient, tmp_path: Path):
    folder = _media_setup(tmp_path)
    [post] = client.get("/candidates/alice/posts").json()
    assert post["post_id"] == "aaa111"
    assert [(f["name"], f["kind"], f["url"]) for f in post["files"]] == [
        ("alice_pics_aaa111_10.jpg", "image", "/posts/aaa111/files/0"),
        ("alice_pics_aaa111_10_2.jpg", "image", "/posts/aaa111/files/1"),
    ]
    response = client.get("/posts/aaa111/files/0")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert len(response.content) == 1000

    partial = client.get("/posts/aaa111/files/0", headers={"Range": "bytes=10-19"})
    assert partial.status_code == 206
    assert partial.content == b"0123456789"

    assert client.get("/posts/aaa111/files/2").status_code == 404
    assert client.get("/posts/nosuch/files/0").status_code == 404
    (folder / "alice_pics_aaa111_10_2.jpg").unlink()
    assert "offline" in client.get("/posts/aaa111/files/1").text


def test_media_key_cookie(tmp_path: Path):
    _configure(tmp_path / "api.db", log_dir=str(tmp_path / "logs"), dest_root=str(tmp_path / "dest"))
    _media_setup(tmp_path)
    keyed = TestClient(create_app(tmp_path / "api.db", api_key="s3cret"))
    assert keyed.get("/posts/aaa111/files/0").status_code == 401
    keyed.cookies.set("bdfrx_manager_key", "wrong")
    assert keyed.get("/posts/aaa111/files/0").status_code == 401
    keyed.cookies.set("bdfrx_manager_key", "s3cret")
    assert keyed.get("/posts/aaa111/files/0").status_code == 200


def test_set_aside_endpoints(client: TestClient, tmp_path: Path):
    folder = _media_setup(tmp_path)
    assert client.get("/set-aside/alice").json() == {"files": 2, "skipped": []}
    assert client.post("/set-aside/alice").json() == {"moved": 2, "skipped": []}
    assert not (folder / "alice_pics_aaa111_10.jpg").exists()
    assert client.get("/set-aside").json()[0]["author"] == "alice"
    # Previews still work from the new location
    assert client.get("/posts/aaa111/files/0").status_code == 200
    assert client.delete("/set-aside/alice").json() == {"moved": 2, "skipped": []}
    assert (folder / "alice_pics_aaa111_10.jpg").exists()
    assert client.delete("/set-aside/alice").status_code == 404


def _wait_for_downloads(client: TestClient, count: int = 1) -> dict:
    """Wait until nothing is running or waiting and `count` jobs have finished; adds 'last' for convenience"""
    for _ in range(400):
        state = client.get("/downloads").json()
        if not state["running"] and not state["pending"] and len(state["recent"]) >= count:
            return {**state, "last": state["recent"][0]}
        time.sleep(0.05)
    raise AssertionError(f"downloads never finished: {state}")


def test_follow_starts_a_download(client: TestClient, fake_bdfrx: Path, tmp_path: Path):  # noqa: F811
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {fake_bdfrx}")
    _media_setup(tmp_path)
    assert client.get("/downloads").json() == {"running": [], "pending": [], "recent": [], "slots": 2}
    promoted = client.post("/candidates/alice/promote").json()
    assert (promoted["status"], promoted["download"]) == ("active", "queued")
    last = _wait_for_downloads(client)["last"]
    assert (last["name"], last["status"], last["files_new"]) == ("alice", "ok", 2)
    run = client.get(f"/runs/{last['run_id']}").json()
    assert "--time all" in run["command"]
    assert client.get("/status").json()["downloads"]["recent"][0]["run_id"] == last["run_id"]
    assert last["trigger"] == "follow"

    no_download = client.post("/candidates/bob/promote", params={"download": False}).json()
    assert no_download["download"] is None


def test_download_endpoint_and_skips(client: TestClient, fake_bdfrx: Path, tmp_path: Path):  # noqa: F811
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {fake_bdfrx}")
    client.post("/sources", json={"kind": "subreddit", "name": "pics"})
    assert client.post("/sources/subreddit/pics/download", params={"time": "week"}).json() == {"download": "queued"}
    last = _wait_for_downloads(client)["last"]
    assert "--time week" in client.get(f"/runs/{last['run_id']}").json()["command"]
    assert client.post("/sources/user/nobody/download").status_code == 404

    # A source removed before its download starts is skipped, not downloaded
    queue = client.app.state.downloads
    assert queue.add("user", "ghost") == "queued"
    last = _wait_for_downloads(client, count=2)["last"]
    assert (last["name"], last["status"]) == ("ghost", "skipped")


def test_follow_of_a_gone_account_reports_deactivation(client: TestClient, fake_bdfrx: Path, tmp_path: Path):  # noqa: F811
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {fake_bdfrx}")
    from bdfrx.manager import candidates

    conn = db.connect(tmp_path / "api.db")
    candidates.record_posts(
        conn,
        [{"post_id": "aaa111", "author": "gone", "subreddit": "pics", "score": 1, "posted_at": None, "files": []}],
    )
    conn.close()
    client.post("/candidates/gone/promote")
    last = _wait_for_downloads(client)["last"]
    assert last["status"] == "deactivated"
    assert "suspended" in last["reason"]
    assert client.get("/sources/user/gone").json()["status"] == "banned"


@pytest.fixture
def slow_bdfrx(tmp_path: Path) -> Path:
    """A stand-in bdfrx that takes a moment and records how many copies of itself run at once"""
    script = tmp_path / "slow_bdfrx.py"
    script.write_text(
        "import fcntl, os, sys, time\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "target = Path(args[1]); target.mkdir(parents=True, exist_ok=True)\n"
        "counter = Path(os.environ['SLOW_COUNTER'])\n"
        "def bump(delta):\n"
        "    with counter.open('a+') as f:\n"
        "        fcntl.flock(f, fcntl.LOCK_EX); f.seek(0); lines = f.read().split()\n"
        "        now = (int(lines[-1].split(':')[0]) if lines else 0) + delta\n"
        "        f.write(f'{now}:{delta}\\n')\n"
        "bump(1); time.sleep(0.6); (target / 'x_pics_abc123_1.jpg').write_text('x'); bump(-1)\n",
    )
    return script


def test_queue_runs_at_most_the_slot_limit_at_once(
    client: TestClient,
    slow_bdfrx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    counter = tmp_path / "concurrency.txt"
    monkeypatch.setenv("SLOW_COUNTER", str(counter))
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {slow_bdfrx}")
    for name in ("a1", "b2", "c3", "d4", "e5"):
        client.post("/sources", json={"kind": "user", "name": name})
    counts = client.post("/runs", json={}).json()
    assert counts == {"queued": 5, "already_queued": 0, "skipped": 0}
    assert client.post("/runs", json={}).json()["already_queued"] >= 3  # ones not yet started are still queued
    state = client.get("/downloads").json()
    assert len(state["running"]) <= 2
    _wait_for_downloads(client, count=5)
    peaks = [int(line.split(":")[0]) for line in counter.read_text().split()]
    assert max(peaks) == 2  # never more than max_concurrent_downloads bdfrx processes at once
    assert {r["status"] for r in client.get("/runs").json()} == {"ok"}


def test_cancel_waiting_downloads(
    client: TestClient,
    slow_bdfrx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SLOW_COUNTER", str(tmp_path / "c.txt"))
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {slow_bdfrx}", max_concurrent_downloads="1")
    for name in ("a1", "b2", "c3", "d4"):
        client.post("/sources", json={"kind": "user", "name": name})
    client.post("/runs", json={})
    time.sleep(0.2)
    pending = client.get("/downloads").json()["pending"]
    assert len(pending) == 3
    assert client.delete(f"/downloads/{pending[0]['id']}").json() == {"cancelled": 1}
    assert client.delete(f"/downloads/{pending[0]['id']}").status_code == 404
    assert client.delete("/downloads").json() == {"cancelled": 2}
    state = _wait_for_downloads(client, count=4)
    assert sorted(r["status"] for r in state["recent"]) == ["cancelled", "cancelled", "cancelled", "ok"]
    assert len(client.get("/runs").json()) == 1


def test_add_source_with_download(client: TestClient, fake_bdfrx: Path, tmp_path: Path):  # noqa: F811
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {fake_bdfrx}")
    added = client.post("/sources", json={"kind": "user", "name": "alice", "download": True}).json()
    assert added["download"] == "queued"
    last = _wait_for_downloads(client)["last"]
    assert (last["trigger"], last["status"]) == ("added", "ok")
    assert "--time all" in client.get(f"/runs/{last['run_id']}").json()["command"]
    assert client.post("/sources", json={"kind": "user", "name": "bob"}).json()["download"] is None
    added = client.post("/sources", json={"kind": "subreddit", "name": "pics", "download": True}).json()
    assert added["download"] == "queued"
    last = _wait_for_downloads(client)["last"]
    assert (last["kind"], last["name"], last["trigger"], last["status"]) == ("subreddit", "pics", "added", "ok")


def test_source_stats(client: TestClient, fake_bdfrx: Path, tmp_path: Path):  # noqa: F811
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {fake_bdfrx}")
    client.post("/sources", json={"kind": "user", "name": "alice"})
    client.post("/sources", json={"kind": "user", "name": "never"})
    client.post("/sources/user/alice/download")
    _wait_for_downloads(client)
    client.post("/sources/user/alice/download")
    _wait_for_downloads(client, count=2)
    stats = {s["name"]: s for s in client.get("/sources").json()}
    assert (stats["alice"]["total_runs"], stats["alice"]["total_files"]) == (2, 2)
    assert stats["alice"]["last_ok_at"] is not None
    assert (stats["never"]["total_runs"], stats["never"]["total_files"], stats["never"]["last_ok_at"]) == (0, 0, None)
    assert client.get("/sources/user/alice").json()["total_runs"] == 2


def test_schedule(tmp_path: Path, fake_bdfrx: Path):  # noqa: F811
    from bdfrx.manager.jobs import DownloadQueue, schedule_due_at, schedule_tick

    _configure(tmp_path / "api.db", log_dir=str(tmp_path / "logs"), dest_root=str(tmp_path / "dest"))
    conn = db.connect(tmp_path / "api.db")
    service.add_source(conn, "user", "alice")
    service.add_source(conn, "user", "sleepy", status="paused")
    queue = DownloadQueue(tmp_path / "api.db", start=False)  # no workers, so jobs stay waiting for the test to see
    assert schedule_due_at(conn) is None
    assert schedule_tick(queue, conn) is None
    service.set_setting(conn, "run_every_hours", "6")
    assert schedule_tick(queue, conn) == {"queued": 1, "already_queued": 0, "skipped": 0}
    assert [(j["name"], j["trigger"]) for j in queue.snapshot()["pending"]] == [("alice", "schedule")]
    assert schedule_tick(queue, conn) is None  # not due again for 6 hours
    next_at = schedule_due_at(conn)
    assert 5.9 * 3600 < (next_at - __import__("datetime").datetime.now(next_at.tzinfo)).total_seconds() <= 6 * 3600
    for bad in ("-1", "0.1", "often"):
        with pytest.raises(service.ManagerError):
            service.set_setting(conn, "run_every_hours", bad)


def _make_media(folder: Path) -> None:
    import subprocess

    folder.mkdir(parents=True, exist_ok=True)
    common = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi"]
    subprocess.run(
        [*common, "-i", "color=c=red:s=800x600", "-frames:v", "1", str(folder / "a_pics_aaa111_1.jpg")],
        check=True,
    )
    subprocess.run(
        [*common, "-i", "testsrc=s=640x360:d=2", "-pix_fmt", "yuv420p", str(folder / "a_pics_bbb222_1.mp4")],
        check=True,
    )
    (folder / "a_pics_ccc333_1.txt").write_text("text posts have no thumbnail")


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")
def test_thumbnails(client: TestClient, tmp_path: Path):
    from bdfrx.manager import thumbs

    folder = tmp_path / "dest" / "subreddits" / "pics"
    _make_media(folder)
    client.post("/sources", json={"kind": "subreddit", "name": "pics"})
    seen = set()
    for _ in range(12):
        response = client.get("/sources/subreddit/pics/thumbnail")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
        seen.add(response.content)
    assert len(seen) == 2  # the image and the video, never the text post
    cached = list((tmp_path / "logs" / "thumbs").rglob("*.jpg"))
    assert len(cached) == 2
    image_thumb = thumbs.thumbnail(folder / "a_pics_aaa111_1.jpg", tmp_path / "logs" / "thumbs")
    assert image_thumb.stat().st_size < 20_000
    with pytest.raises(thumbs.ThumbnailError):
        thumbs.thumbnail(folder / "a_pics_ccc333_1.txt", tmp_path / "logs" / "thumbs")

    from bdfrx.manager import candidates

    conn = db.connect(tmp_path / "api.db")
    candidates.scan_archive(conn, tmp_path / "dest")
    conn.close()
    assert client.get("/posts/bbb222/files/0/thumbnail").headers["content-type"] == "image/jpeg"
    assert client.get("/posts/ccc333/files/0/thumbnail").status_code == 404

    client.post("/sources", json={"kind": "subreddit", "name": "empty"})
    assert client.get("/sources/subreddit/empty/thumbnail").status_code == 404


# Surviving restarts


def test_queue_survives_a_restart(tmp_path: Path):
    from bdfrx.manager.jobs import DownloadQueue

    _configure(tmp_path / "api.db", log_dir=str(tmp_path / "logs"), dest_root=str(tmp_path / "dest"))
    first = DownloadQueue(tmp_path / "api.db", start=False)
    assert first.add("user", "alice", "all", trigger="follow") == "queued"
    assert first.add("user", "ALICE") == "already queued"
    first.add("user", "bob")
    first.cancel(first.snapshot()["pending"][1]["id"])

    second = DownloadQueue(tmp_path / "api.db", start=False)  # a new server process
    [waiting] = second.snapshot()["pending"]
    assert (waiting["name"], waiting["time"], waiting["trigger"]) == ("alice", "all", "follow")
    assert second.snapshot()["recent"][0]["status"] == "cancelled"


def test_interrupted_download_resumes_after_restart(tmp_path: Path, fake_bdfrx: Path):  # noqa: F811
    _configure(
        tmp_path / "api.db",
        log_dir=str(tmp_path / "logs"),
        dest_root=str(tmp_path / "dest"),
        staging_root=str(tmp_path / "staging"),
        bdfrx_command=f"{sys.executable} {fake_bdfrx}",
    )
    conn = db.connect(tmp_path / "api.db")
    alice = service.add_source(conn, "user", "alice")
    # What a server killed mid-download leaves behind: a running job, a running run, and staged files
    conn.execute(
        "INSERT INTO jobs (kind, name, time, trigger, state, started_at) VALUES ('user', 'alice', 'all', 'follow', "
        "'running', datetime('now'));",
    )
    log_path = tmp_path / "logs" / "old.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("downloading...\n")
    conn.execute(
        "INSERT INTO runs (source_id, source_kind, source_name, started_at, log_path) "
        "VALUES (?, 'user', 'alice', datetime('now'), ?);",
        (alice["id"], str(log_path)),
    )
    conn.commit()
    staged = tmp_path / "staging" / "users" / "alice"
    staged.mkdir(parents=True)
    (staged / "alice_pics_old111_3.jpg").write_text("downloaded before the crash")
    conn.close()

    client = TestClient(create_app(tmp_path / "api.db", schedule=False))
    state = _wait_for_downloads(client)
    last = state["last"]
    assert (last["name"], last["status"], last["resumed"]) == ("alice", "ok", 1)
    assert "--time all" in client.get(f"/runs/{last['run_id']}").json()["command"]
    old_run = client.get("/runs", params={"name": "alice"}).json()[-1]
    assert old_run["status"] == "failed"
    assert "interrupted" in log_path.read_text()
    archive = tmp_path / "dest" / "users" / "alice"
    assert (archive / "alice_pics_old111_3.jpg").read_text() == "downloaded before the crash"
    assert (archive / "alice_post1.jpg").exists()
    assert not staged.exists()


def test_runs_page_endpoints(client: TestClient, fake_bdfrx: Path, tmp_path: Path):  # noqa: F811
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {fake_bdfrx}")
    client.post("/sources", json={"kind": "user", "name": "alice"})
    client.post("/sources", json={"kind": "user", "name": "broken"})
    client.post("/sources/user/alice/download", params={"time": "week"})
    _wait_for_downloads(client)
    client.post("/sources/user/broken/download")
    _wait_for_downloads(client, count=2)

    stats = client.get("/runs/stats", params={"days": 3}).json()
    assert len(stats["per_day"]) == 3
    assert stats["last_24h"]["runs"] == 2
    assert stats["logs"]["files"] == 2
    assert [r["source_name"] for r in client.get("/runs/attention").json()["failed_runs"]] == ["broken"]
    assert [r["source_name"] for r in client.get("/runs", params={"status": "failed", "since": "24h"}).json()] == [
        "broken"
    ]
    assert {r["trigger"] for r in client.get("/runs", params={"trigger": "manual"}).json()} == {"manual"}
    first = client.get("/runs").json()[-1]
    assert [r["id"] for r in client.get("/runs", params={"before_id": first["id"] + 1}).json()] == [first["id"]]

    again = client.post(f"/runs/{first['id']}/again").json()
    assert again == {"download": "queued", "time": "week"}
    last = _wait_for_downloads(client, count=3)["last"]
    assert "--time week" in client.get(f"/runs/{last['run_id']}").json()["command"]


def test_feed_and_user_subreddits_endpoints(client: TestClient, tmp_path: Path):
    from bdfrx.manager import candidates

    conn = db.connect(tmp_path / "api.db")
    service.add_source(conn, "user", "alice")
    run_id = conn.execute(
        "INSERT INTO runs (source_kind, source_name, started_at, status) VALUES ('user', 'alice', datetime('now'), 'ok');",
    ).lastrowid
    conn.commit()
    candidates.record_posts(
        conn,
        [
            {
                "post_id": "aaa111",
                "author": "alice",
                "subreddit": "cats",
                "score": 3,
                "posted_at": None,
                "files": ["/x/a.jpg"],
            }
        ],
        run_id,
    )
    conn.close()
    [post] = client.get("/feed").json()
    assert (post["author"], post["subreddit"], post["source_name"]) == ("alice", "cats", "alice")
    assert post["files"][0]["url"] == "/posts/aaa111/files/0"
    assert client.get("/sources/user/alice/subreddits").json()[0]["name"] == "cats"
    assert client.get("/sources/user/nobody/subreddits").status_code == 404


def test_feed_groups_endpoint(client: TestClient, tmp_path: Path):
    from bdfrx.manager import candidates

    conn = db.connect(tmp_path / "api.db")
    run_id = conn.execute(
        "INSERT INTO runs (source_kind, source_name, started_at, status) VALUES ('subreddit', 'pics', datetime('now'), 'ok');",
    ).lastrowid
    conn.commit()
    candidates.record_posts(
        conn,
        [
            {
                "post_id": "p1xxx1",
                "author": "zed",
                "subreddit": "pics",
                "score": 1,
                "posted_at": None,
                "files": ["/x/p.jpg"],
            }
        ],
        run_id,
    )
    conn.close()
    assert [(s["kind"], s["name"], s["posts"]) for s in client.get("/feed/sources").json()] == [
        ("subreddit", "pics", 1)
    ]
    groups = client.get("/feed/groups", params={"per_source": 5}).json()
    assert groups["total"] == 1
    assert groups["groups"][0]["items"][0]["files"][0]["url"] == "/posts/p1xxx1/files/0"
    assert client.get("/feed/sources", params={"kind": "user"}).json() == []


def test_requested_downloads_go_ahead_of_bulk_ones(tmp_path: Path):
    from bdfrx.manager.jobs import DownloadQueue, _wider_time

    _configure(tmp_path / "api.db", log_dir=str(tmp_path / "logs"), dest_root=str(tmp_path / "dest"))
    queue = DownloadQueue(tmp_path / "api.db", start=False)
    for name in ("a1", "b2", "c3"):
        queue.add("subreddit", name, trigger="run-all")
    assert queue.add("user", "fresh", "all", trigger="follow") == "queued"
    assert queue.add("subreddit", "d4", trigger="schedule") == "queued"
    assert queue.add("subreddit", "e5", "week", trigger="manual") == "queued"
    order = [j["name"] for j in queue.snapshot()["pending"]]
    assert order == ["fresh", "e5", "a1", "b2", "c3", "d4"]

    # Following someone already waiting in a bulk run moves them up, with the wider time window, behind requests
    # made before it
    time.sleep(1.1)  # queued_at has one-second resolution
    assert queue.add("subreddit", "c3", "all", trigger="follow") == "moved up"
    pending = queue.snapshot()["pending"]
    assert [j["name"] for j in pending][:3] == ["fresh", "e5", "c3"]
    moved = next(j for j in pending if j["name"] == "c3")
    assert (moved["time"], moved["trigger"], moved["priority"]) == ("all", "follow", 1)
    assert queue.add("subreddit", "c3", "week", trigger="manual") == "already queued"  # already at the front

    # The worker takes them in that order
    conn = db.connect(tmp_path / "api.db")
    assert queue._claim(conn)["name"] == "fresh"
    assert queue.add("user", "fresh", trigger="manual") == "already queued"  # running now: left alone
    conn.close()

    assert _wider_time("week", "all") == "all"
    assert _wider_time("month", "day") == "month"
    assert _wider_time(None, "day") == "day"
    assert _wider_time("week", None) == "week"


@pytest.fixture
def limited_bdfrx(tmp_path: Path) -> Path:
    """A stand-in bdfrx that downloads one file, then stops because a host keeps refusing with HTTP 429"""
    script = tmp_path / "limited_bdfrx.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "target = Path(args[1]); target.mkdir(parents=True, exist_ok=True)\n"
        "(target / 'x_pics_abc123_1.jpg').write_text('x')\n"
        "print('[x - bdfrx.__main__ - ERROR] - Stopped: rate limited by i.imgur.com; the rest of this run')\n"
        "sys.exit(75)\n",
    )
    return script


def test_rate_limited_download_goes_to_the_back_and_waits(tmp_path: Path, limited_bdfrx: Path):
    from bdfrx.manager import insights, runner
    from bdfrx.manager.jobs import DownloadQueue

    _configure(
        tmp_path / "api.db",
        log_dir=str(tmp_path / "logs"),
        dest_root=str(tmp_path / "dest"),
        bdfrx_command=f"{sys.executable} {limited_bdfrx}",
        rate_limit_backoff_minutes="30",
    )
    conn = db.connect(tmp_path / "api.db")
    service.add_source(conn, "subreddit", "pics")
    service.add_source(conn, "subreddit", "cats")
    run = runner.run_one(conn, "subreddit", "pics")
    assert (run["status"], run["rate_limited"], run["files_new"]) == ("failed", "i.imgur.com", 1)

    queue = DownloadQueue(tmp_path / "api.db", start=False)
    queue.add("subreddit", "pics", "all", trigger="follow")
    queue.add("subreddit", "cats", trigger="run-all")
    job = queue._claim(conn)
    assert job["name"] == "pics"
    queue._run(conn, job)
    state = queue.snapshot()
    assert state["recent"][0]["status"] == "rate-limited"
    assert "i.imgur.com" in state["recent"][0]["reason"]
    assert [j["name"] for j in state["pending"]] == ["cats", "pics"]  # back of the queue
    requeued = state["pending"][1]
    assert (requeued["priority"], requeued["time"], requeued["trigger"]) == (0, "all", "follow")
    assert requeued["not_before"] > requeued["queued_at"]

    assert queue._claim(conn)["name"] == "cats"
    assert queue._claim(conn) is None  # pics must wait out the backoff
    # Asking for it again by hand puts it at the front and lifts the wait
    assert queue.add("subreddit", "pics", trigger="manual") == "moved up"
    assert queue._claim(conn)["name"] == "pics"
    assert any(run["rate_limited"] for run in insights.attention(conn)["failed_runs"])
    with pytest.raises(service.ManagerError):
        service.set_setting(conn, "rate_limit_backoff_minutes", "0")
    conn.close()


@pytest.fixture
def pacific_time(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_schedule_at_a_fixed_local_time(tmp_path: Path, pacific_time):  # noqa: ARG001
    from datetime import datetime, timezone

    from bdfrx.manager.jobs import schedule_due_at

    _configure(tmp_path / "api.db", log_dir=str(tmp_path / "logs"), dest_root=str(tmp_path / "dest"))
    conn = db.connect(tmp_path / "api.db")
    service.set_setting(conn, "run_every_hours", "24")
    service.set_setting(conn, "run_at", "01:00")

    def local(text: str) -> datetime:  # "2026-09-30 20:25" Pacific -> UTC
        return datetime.strptime(text, "%Y-%m-%d %H:%M").astimezone().astimezone(timezone.utc)

    def last_ran(text: str) -> None:
        service.set_state(conn, "last_scheduled_at", local(text).strftime("%Y-%m-%d %H:%M:%S"))

    def due(now: str) -> datetime:
        return schedule_due_at(conn, now=local(now))

    # Turned on in the evening: the first run is 1 AM, not right away
    assert due("2026-09-30 20:25") == local("2026-10-01 01:00")
    last_ran("2026-10-01 01:00")
    assert due("2026-10-01 05:00") == local("2026-10-02 01:00")
    # Down at 1 AM, back at 2:30: catches up; back at 6 AM: waits for the next night
    assert due("2026-10-02 02:30") == local("2026-10-02 01:00")
    assert due("2026-10-02 06:00") == local("2026-10-03 01:00")
    # Daylight saving ends on 1 November: still 1 AM local on either side
    last_ran("2026-10-31 01:00")
    assert due("2026-10-31 12:00") == local("2026-11-01 01:00")
    last_ran("2026-11-01 01:00")
    nov2 = due("2026-11-01 12:00")
    assert nov2 == local("2026-11-02 01:00")
    assert nov2.astimezone().hour == 1
    # Every 6 hours from 01:00: 01, 07, 13, 19
    service.set_setting(conn, "run_every_hours", "6")
    last_ran("2026-10-05 07:00")
    assert due("2026-10-05 08:00") == local("2026-10-05 13:00")
    last_ran("2026-10-05 19:00")
    assert due("2026-10-05 20:00") == local("2026-10-06 01:00")
    # Without a time, it counts from the last run as before
    service.set_setting(conn, "run_at", None)
    assert due("2026-10-05 20:00") == local("2026-10-06 01:00")
    for bad in ("1am", "24:00", "7:5"):
        with pytest.raises(service.ManagerError):
            service.set_setting(conn, "run_at", bad)
    conn.close()


def test_source_gallery(client: TestClient, tmp_path: Path):
    from bdfrx.manager import gallery

    folder = tmp_path / "dest" / "users" / "alice"
    folder.mkdir(parents=True)
    files = {
        "alice_cats_1aaaaa_5.jpg": "old",
        "alice_dog_pics_1zzzzz_9_1.mp4": "new gallery 1",
        "alice_dog_pics_1zzzzz_9_2.jpg": "new gallery 2",
        "alice_u_alice_1mmmmm.png": "middle",
        "notes.txt": "not in the scheme",
        ".hidden": "skipped",
    }
    for file_name, content in files.items():
        (folder / file_name).write_text(content)
    (tmp_path / "secret.txt").write_text("outside the folder")
    client.post("/sources", json={"kind": "user", "name": "alice"})

    page = client.get("/sources/user/alice/gallery").json()
    assert (page["total_posts"], page["total_files"]) == (4, 5)
    assert [p["post_id"] for p in page["posts"]] == ["1zzzzz", "1mmmmm", "1aaaaa", None]
    newest = page["posts"][0]
    assert (newest["subreddit"], newest["score"]) == ("dog_pics", 9)
    assert [(f["index"], f["kind"]) for f in newest["files"]] == [(0, "video"), (1, "image")]
    assert newest["files"][1]["url"] == "/sources/user/alice/files/alice_dog_pics_1zzzzz_9_2.jpg"
    assert client.get(newest["files"][1]["url"]).text == "new gallery 2"

    oldest = client.get("/sources/user/alice/gallery", params={"order": "oldest", "limit": 1}).json()
    assert oldest["posts"][0]["post_id"] is None  # names outside the scheme sort last, so first when reversed
    videos = client.get("/sources/user/alice/gallery", params={"media": "videos"}).json()
    assert [p["post_id"] for p in videos["posts"]] == ["1zzzzz"]
    assert (
        client.get("/sources/user/alice/gallery", params={"offset": 1, "limit": 1}).json()["posts"][0]["post_id"]
        == "1mmmmm"
    )

    # Only files directly inside the source's own folder
    for bad in ("..%2Fsecret.txt", "%2E%2E%2F%2E%2E%2Fsecret.txt", ".hidden", "nope.jpg"):
        assert client.get(f"/sources/user/alice/files/{bad}").status_code == 404, bad
    assert gallery.file_in_folder(folder, "../../secret.txt") is None
    assert client.get("/sources/user/nobody/gallery").status_code == 404


def test_gallery_statuses(client: TestClient, tmp_path: Path):
    folder = tmp_path / "dest" / "subreddits" / "pics"
    folder.mkdir(parents=True)
    for name in ("Alice_pics_aaa111_10.jpg", "bob_pics_bbb111_1.jpg", "carol_pics_ccc111_2.jpg", "dave_pics_ddd111.jpg"):
        (folder / name).write_text("x")
    client.post("/sources", json={"kind": "subreddit", "name": "pics"})
    client.post("/sources", json={"kind": "user", "name": "alice"})
    client.post("/sources", json={"kind": "user", "name": "bob", "status": "paused"})
    client.post("/rules", json={"kind": "ignore_user", "value": "carol"})
    client.post("/rules", json={"kind": "skip_subreddit", "value": "cats"})

    posts = {p["author"]: p for p in client.get("/sources/subreddit/pics/gallery").json()["posts"]}
    statuses = {a: (p["author_status"], p["author_ignored"]) for a, p in posts.items()}
    assert statuses == {"Alice": ("active", False), "bob": ("paused", False), "carol": (None, True), "dave": (None, False)}
    assert (posts["dave"]["subreddit_status"], posts["dave"]["subreddit_skipped"]) == ("active", False)

    user_folder = tmp_path / "dest" / "users" / "alice"
    user_folder.mkdir(parents=True)
    (user_folder / "alice_cats_eee111_1.jpg").write_text("x")
    post = client.get("/sources/user/alice/gallery").json()["posts"][0]
    assert (post["subreddit"], post["subreddit_status"], post["subreddit_skipped"]) == ("cats", None, True)


def test_set_aside_gallery(client: TestClient, tmp_path: Path):
    from bdfrx.manager import candidates

    root = tmp_path / "dest"
    for relative in (
        "subreddits/pics/alice_pics_1aaaaa_10.jpg",
        "subreddits/pics/alice_pics_1zzzzz_10_1.jpg",
        "subreddits/pics/alice_pics_1zzzzz_10_2.mp4",
        "subreddits/pics/bob_pics_1bbbbb_1.jpg",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    conn = db.connect(tmp_path / "api.db")
    candidates.scan_archive(conn, root)
    conn.close()
    client.post("/rules", json={"kind": "ignore_user", "value": "alice"})
    client.post("/set-aside/alice")
    client.post("/set-aside/bob")

    page = client.get("/set-aside/alice/gallery").json()
    assert (page["total_posts"], page["total_files"]) == (2, 3)
    newest = page["posts"][0]
    assert (newest["post_id"], newest["subreddit"]) == ("1zzzzz", "pics")
    assert [f["kind"] for f in newest["files"]] == ["image", "video"]
    url = newest["files"][0]["url"]
    assert client.get(url).text == "subreddits/pics/alice_pics_1zzzzz_10_1.jpg"
    assert client.get("/set-aside/alice/gallery", params={"media": "videos"}).json()["total_posts"] == 1

    bob_id = client.get("/set-aside/bob/gallery").json()["posts"][0]["files"][0]["id"]
    assert client.get(f"/set-aside/alice/files/{bob_id}").status_code == 404  # not alice's
    assert client.get("/set-aside/alice/files/99999").status_code == 404
    client.delete("/set-aside/alice")
    assert client.get(url).status_code == 404  # restored, so no longer served from here
    assert client.get("/set-aside/alice/gallery").json()["total_posts"] == 0
