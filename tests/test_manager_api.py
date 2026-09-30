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


def _wait_for_downloads(client: TestClient) -> dict:
    for _ in range(200):
        state = client.get("/downloads").json()
        if not state["current"] and not state["pending"] and state["last"]:
            return state
        time.sleep(0.05)
    raise AssertionError(f"downloads never finished: {state}")


def test_follow_starts_a_download(client: TestClient, fake_bdfrx: Path, tmp_path: Path):  # noqa: F811
    _configure(tmp_path / "api.db", bdfrx_command=f"{sys.executable} {fake_bdfrx}")
    _media_setup(tmp_path)
    assert client.get("/downloads").json() == {"current": None, "pending": [], "last": None}
    promoted = client.post("/candidates/alice/promote").json()
    assert (promoted["status"], promoted["download"]) == ("active", "queued")
    last = _wait_for_downloads(client)["last"]
    assert (last["name"], last["status"], last["files_new"]) == ("alice", "ok", 2)
    run = client.get(f"/runs/{last['run_id']}").json()
    assert "--time all" in run["command"]
    assert client.get("/status").json()["downloads"]["last"]["run_id"] == last["run_id"]

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
    for _ in range(100):
        if queue.snapshot()["last"]["name"] == "ghost":
            break
        time.sleep(0.05)
    assert queue.snapshot()["last"]["status"] == "skipped"


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
