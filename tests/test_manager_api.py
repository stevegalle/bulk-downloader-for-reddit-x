import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from bdfrx.manager.api import create_app
from tests.test_manager import fake_bdfrx  # noqa: F401


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    test_client = TestClient(create_app(tmp_path / "api.db"))
    for key, value in (("log_dir", tmp_path / "logs"), ("dest_root", tmp_path / "dest")):
        assert test_client.put(f"/settings/{key}", json={"value": str(value)}).status_code == 200
    return test_client


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
    assert settings["default_sort"] == {"value": "new", "default": "new", "help": settings["default_sort"]["help"]}
    assert client.put("/settings/default_sort", json={"value": "top"}).json() == {"default_sort": "top"}
    assert client.put("/settings/default_sort", json={"value": None}).json() == {"default_sort": "new"}
    assert client.put("/settings/nope", json={"value": "x"}).status_code == 400


def test_runs(client: TestClient, fake_bdfrx: Path):  # noqa: F811
    client.put("/settings/bdfrx_command", json={"value": f"{sys.executable} {fake_bdfrx}"})
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
    from bdfrx.manager import candidates, db

    conn = db.connect(tmp_path / "api.db")
    candidates.scan_archive(conn, tmp_path / "dest")
    conn.close()

    assert [c["name"] for c in client.get("/candidates").json()] == ["alice", "bob"]
    assert [c["name"] for c in client.get("/candidates", params={"min_posts": 2}).json()] == ["alice"]
    assert client.get("/candidates", params={"since": "whenever"}).status_code == 400
    assert client.put("/candidates/bob/dismissed", json={"reason": "meh"}).status_code == 204
    assert client.get("/candidates/dismissed").json()[0]["reason"] == "meh"
    assert client.post("/candidates/alice/promote").json()["status"] == "active"
    assert client.get("/candidates").json() == []
    assert client.delete("/candidates/bob/dismissed").status_code == 204
    assert client.delete("/candidates/bob/dismissed").status_code == 404
