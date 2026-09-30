import os
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdfrx.manager import candidates, db, history, importer, runner, service, setaside
from bdfrx.manager.cli import cli


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "manager.db")
    service.set_setting(connection, "log_dir", str(tmp_path / "logs"))
    service.set_setting(connection, "dest_root", str(tmp_path / "dest"))
    yield connection
    connection.close()


@pytest.fixture
def fake_bdfrx(tmp_path: Path) -> Path:
    """Stands in for bdfrx: writes one file per run into the download dir, and can pretend a user is suspended"""
    script = tmp_path / "fake_bdfrx.py"
    script.write_text(
        textwrap.dedent(
            """
            import os
            import sys
            from pathlib import Path
            args = sys.argv[1:]
            target = Path(args[1])
            name = args[args.index("--user") + 1] if "--user" in args else args[args.index("--subreddit") + 1]
            if name == "gone":
                print(f"[x - bdfrx.connector - ERROR] - User {name} is banned")
                sys.exit(0)
            if name == "broken":
                sys.exit(3)
            target.mkdir(parents=True, exist_ok=True)
            if "--subreddit" in args:
                (target / f"Poster_One_{name}_p0st01_12.jpg").write_text("x")
                (target / f"Poster_One_{name}_p0st01_12_2.jpg").write_text("x")
            (target / f"{name}_post1.jpg").write_text("x")
            (target / "shared.jpg").write_text("new")
            print("ARGS", " ".join(args))
            print("CWD", os.getcwd())
            """,
        ),
    )
    return script


def use_fake(conn: sqlite3.Connection, script: Path) -> None:
    service.set_setting(conn, "bdfrx_command", f"{sys.executable} {script}")


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    (
        ("user", "u/Some_User", "Some_User"),
        ("user", "/user/Some-User/", "Some-User"),
        ("subreddit", "r/pics", "pics"),
    ),
)
def test_normalize_name(kind: str, raw: str, expected: str):
    assert service.normalize_name(kind, raw) == expected


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    (
        ("exclude_post", "https://www.reddit.com/r/pics/comments/Ab12cd/some_title/", "ab12cd"),
        ("exclude_post", "https://redd.it/ab12cd", "ab12cd"),
        ("exclude_post", "ab12cd", "ab12cd"),
        ("skip_domain", "https://Example.com/path", "example.com"),
        ("skip_subreddit", "r/pics", "pics"),
    ),
)
def test_normalize_rule_value(kind: str, raw: str, expected: str):
    assert service.normalize_rule_value(kind, raw) == expected


@pytest.mark.parametrize(
    ("kind", "raw"),
    (("exclude_post", "not a post!"), ("skip_domain", "nodot"), ("ignore_user", "a b")),
)
def test_normalize_rejects(kind: str, raw: str):
    with pytest.raises(service.ManagerError):
        service.normalize_rule_value(kind, raw)


def test_add_and_update_source(conn: sqlite3.Connection):
    service.add_source(conn, "user", "u/Alice", sort="top")
    with pytest.raises(service.ManagerError):
        service.add_source(conn, "user", "alice")
    source = service.update_source(conn, "user", "ALICE", min_score=5, sort=None)
    assert source["name"] == "Alice"
    assert source["sort"] is None
    assert source["min_score"] == 5
    with pytest.raises(service.ManagerError):
        service.update_source(conn, "user", "Alice", sort="sideways")
    with pytest.raises(service.ManagerError):
        service.update_source(conn, "user", "Alice", status=None)
    with pytest.raises(service.NotFoundError):
        service.get_source(conn, "user", "bob")


def test_ignore_user_pauses_source(conn: sqlite3.Connection):
    service.add_source(conn, "user", "alice")
    service.ignore_user(conn, "u/alice", "spam")
    assert service.get_source(conn, "user", "alice")["status"] == "paused"
    assert service.list_rules(conn, "ignore_user")[0]["reason"] == "spam"


def test_settings(conn: sqlite3.Connection):
    service.set_setting(conn, "default_sort", "top")
    assert service.get_settings(conn)["default_sort"] == "top"
    service.set_setting(conn, "default_sort", None)
    assert service.get_settings(conn)["default_sort"] == "new"
    with pytest.raises(service.ManagerError):
        service.set_setting(conn, "no_such_setting", "x")
    with pytest.raises(service.ManagerError):
        service.set_setting(conn, "default_time", "fortnight")


def test_import_directories(conn: sqlite3.Connection, tmp_path: Path):
    root = tmp_path / "data_local"
    for path in (
        "users/alice",
        "users/bob",
        "users_backup/alice",
        "users_backup/carol",
        "banned/dave",
        "users/dave",
        "subreddits/pics",
        "users/bad name",
    ):
        (root / path).mkdir(parents=True)
    report = importer.import_directories(conn, root)
    statuses = {(s["kind"], s["name"]): s["status"] for s in service.list_sources(conn)}
    assert statuses == {
        ("user", "alice"): "active",
        ("user", "bob"): "active",
        ("user", "carol"): "paused",
        ("user", "dave"): "banned",
        ("subreddit", "pics"): "active",
    }
    assert report["invalid"] == ["users/bad name"]
    assert len(importer.import_directories(conn, root)["existing"]) == 5


def test_build_command(conn: sqlite3.Connection):
    service.add_source(conn, "user", "alice", filter_gender="none", min_score=3, extra_args="--max-wait-time 5")
    service.add_source(conn, "subreddit", "pics")
    settings = service.get_settings(conn)
    rules = {"ignore_user": ["spammer"], "skip_domain": ["example.com"], "skip_subreddit": ["boring"]}
    user_cmd = runner.build_command(
        settings,
        service.get_source(conn, "user", "alice"),
        rules,
        {"exclude_post": Path("ids.txt")},
    )
    start = runner.default_bdfrx_command()
    assert user_cmd[: len(start)] == start
    assert user_cmd[len(start) : len(start) + 3] == [
        "download",
        str(Path(settings["dest_root"], "users", "alice")),
        "--user",
    ]
    assert "--submitted" in user_cmd
    assert "--filter-gender" not in user_cmd
    assert user_cmd[user_cmd.index("--min-score") + 1] == "3"
    assert user_cmd[user_cmd.index("--db-file") + 1].endswith("/.config/bdfrx/userdbs/alice.db")
    assert user_cmd[user_cmd.index("--exclude-id-file") + 1] == "ids.txt"
    assert user_cmd[-2:] == ["--max-wait-time", "5"]

    sub_cmd = runner.build_command(settings, service.get_source(conn, "subreddit", "pics"), rules, sort_override="top")
    assert "--submitted" not in sub_cmd
    assert "--db-file" not in sub_cmd
    assert sub_cmd[sub_cmd.index("--sort") + 1] == "top"
    assert sub_cmd[sub_cmd.index("--filter-gender") + 1] == "male"
    for flag, value in (("--ignore-user", "spammer"), ("--skip-domain", "example.com"), ("--skip-subreddit", "boring")):
        assert sub_cmd[sub_cmd.index(flag) + 1] == value


def test_run_direct(conn: sqlite3.Connection, fake_bdfrx: Path, tmp_path: Path):
    use_fake(conn, fake_bdfrx)
    service.add_source(conn, "user", "alice")
    service.add_source(conn, "user", "paused_one", status="paused")
    service.add_rule(conn, "exclude_post", "abc123")

    results = runner.run(conn)
    assert [r["source_name"] for r in results] == ["alice"]
    assert results[0]["status"] == "ok"
    assert results[0]["files_new"] == 2
    assert (tmp_path / "dest" / "users" / "alice" / "alice_post1.jpg").exists()
    assert (tmp_path / "logs" / "exclude_ids.txt").read_text() == "abc123\n"
    assert not (tmp_path / "logs" / "ignore_users.txt").exists()
    assert "--exclude-id-file" in Path(results[0]["log_path"]).read_text()

    # A second run finds nothing new, since the fake rewrites the same files
    assert runner.run(conn)[0]["files_new"] == 0
    assert service.get_source(conn, "user", "alice")["last_status"] == "ok"


def test_run_staged(conn: sqlite3.Connection, fake_bdfrx: Path, tmp_path: Path):
    use_fake(conn, fake_bdfrx)
    service.set_setting(conn, "staging_root", str(tmp_path / "staging"))
    service.add_source(conn, "user", "alice")
    existing = tmp_path / "dest" / "users" / "alice" / "shared.jpg"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")

    result = runner.run(conn)[0]
    assert (result["files_new"], result["files_skipped"]) == (1, 1)
    assert existing.read_text() == "old"
    assert (existing.parent / "alice_post1.jpg").exists()
    assert not list((tmp_path / "staging").rglob("*.jpg"))


def test_move_staged_when_timestamps_cannot_be_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    staging, dest = tmp_path / "staging", tmp_path / "dest"
    staging.mkdir()
    (staging / "a.jpg").write_text("a")
    os.utime(staging / "a.jpg", (1_700_000_000, 1_700_000_000))

    def no_rename(self: Path, target: Path) -> None:
        raise OSError("cross-device link")

    def no_utime(*args, **kwargs) -> None:
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(Path, "rename", no_rename)
    monkeypatch.setattr(runner.os, "utime", no_utime)
    assert runner.move_staged(staging, dest) == (1, 0)
    assert (dest / "a.jpg").read_text() == "a"
    assert not (staging / "a.jpg").exists()


def test_move_staged_keeps_post_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    staging, dest = tmp_path / "staging", tmp_path / "dest"
    staging.mkdir()
    (staging / "a.jpg").write_text("a")
    os.utime(staging / "a.jpg", (1_700_000_000, 1_700_000_000))
    monkeypatch.setattr(Path, "rename", lambda self, target: (_ for _ in ()).throw(OSError("cross-device")))
    runner.move_staged(staging, dest)
    assert (dest / "a.jpg").stat().st_mtime == 1_700_000_000


def test_run_failures_and_suspended_users(conn: sqlite3.Connection, fake_bdfrx: Path):
    use_fake(conn, fake_bdfrx)
    service.add_source(conn, "user", "gone")
    service.add_source(conn, "user", "broken")
    results = {r["source_name"]: r for r in runner.run(conn)}
    assert results["broken"]["status"] == "failed"
    assert results["broken"]["exit_code"] == 3
    assert service.get_source(conn, "user", "broken")["consecutive_failures"] == 1
    gone = service.get_source(conn, "user", "gone")
    assert gone["status"] == "banned"
    assert "suspended" in gone["note"]
    assert results["gone"]["deactivated"] == gone["note"]
    assert results["broken"]["deactivated"] is None
    # Deactivated accounts are left out of later runs
    assert "gone" not in [r["source_name"] for r in runner.run(conn)]


def test_run_skips_by_rule_and_orders_by_last_run(conn: sqlite3.Connection, fake_bdfrx: Path):
    use_fake(conn, fake_bdfrx)
    for name in ("alice", "bob", "carol"):
        service.add_source(conn, "user", name)
    service.add_source(conn, "subreddit", "pics")
    service.add_rule(conn, "skip_subreddit", "pics")
    runner.run(conn, names=["bob"])
    plan = runner.run(conn, dry_run=True)
    assert [p["name"] for p in plan][-1] == "bob"
    assert next(p for p in plan if p["name"] == "pics")["skipped"] == "subreddit is skipped"


def test_select_sources_by_prefixed_name(conn: sqlite3.Connection):
    service.add_source(conn, "user", "Same")
    service.add_source(conn, "subreddit", "same")
    service.add_source(conn, "user", "other")

    def selected(*names: str) -> list[tuple[str, str]]:
        return sorted((s["kind"], s["name"]) for s in runner.select_sources(conn, names=list(names)))

    assert selected("same") == [("subreddit", "same"), ("user", "Same")]
    assert selected("u/same") == [("user", "Same")]
    assert selected("/user/SAME/") == [("user", "Same")]
    assert selected("r/same") == [("subreddit", "same")]
    assert selected("r/same", "other") == [("subreddit", "same"), ("user", "other")]
    assert selected("r/other") == []


def test_run_lock(conn: sqlite3.Connection, tmp_path: Path):
    service.add_source(conn, "user", "alice")
    with runner.run_lock(tmp_path / "logs"):
        assert runner.is_locked(tmp_path / "logs")
        with pytest.raises(runner.RunLockedError):
            runner.run(conn)
    assert not runner.is_locked(tmp_path / "logs")


def test_cli(tmp_path: Path, fake_bdfrx: Path):
    cli_runner = CliRunner()
    db_args = ["--db", str(tmp_path / "cli.db")]

    def invoke(*args: str) -> str:
        result = cli_runner.invoke(cli, [*db_args, *args], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        return result.output

    invoke("config", "set", "log_dir", str(tmp_path / "logs"))
    invoke("config", "set", "dest_root", str(tmp_path / "dest"))
    invoke("config", "set", "bdfrx_command", f"{sys.executable} {fake_bdfrx}")
    invoke("add", "user", "alice", "u/bob", "--sort", "top")
    invoke("set", "user", "alice", "--sort", "default", "--min-score", "4")
    invoke("ignore", "bob", "--reason", "spam")
    invoke("exclude", "https://redd.it/xyz789")
    listing = invoke("list")
    assert "alice" in listing
    assert "min_score=4" in listing
    assert "paused" in listing.split("bob")[1].splitlines()[0]
    plan = invoke("run", "--dry-run")
    assert f"--ignore-user-file {tmp_path / 'logs' / 'ignore_users.txt'}" in plan
    assert "--ignore-user bob" not in plan
    assert "1 source(s) run, 0 failed, 2 new file(s)" in invoke("run")
    assert "alice" in invoke("runs")
    assert "ARGS" in invoke("log", "1")
    bad = cli_runner.invoke(cli, [*db_args, "set", "user", "alice", "--min-score", "lots"])
    assert bad.exit_code != 0


# Candidates


@pytest.mark.parametrize(
    ("file_name", "subreddit", "expected"),
    (
        ("zoey_blowie_18_19_1wjikf4_51.mp4", "18_19", ("zoey_blowie", "1wjikf4", 51)),
        ("Twilight_Floral_Mist_18_19_1l3u71c.jpeg", "18_19", ("Twilight_Floral_Mist", "1l3u71c", None)),
        ("Tetrad_shibari_1wls15c_585_2.jpg", "shibari", ("Tetrad", "1wls15c", 585)),
        ("54Recent-Condition45_18_19_1tio7ox_5.jpeg", "18_19", ("54Recent-Condition45", "1tio7ox", 5)),
        ("zvayot7749_gonewildaudio_1wnmh0b_15.txt", "GoneWildAudio", ("zvayot7749", "1wnmh0b", 15)),
        ("DELETED_pics_abc123_-4.png", "pics", ("DELETED", "abc123", -4)),
    ),
)
def test_parse_file_name(file_name: str, subreddit: str, expected: tuple):
    post = candidates.parse_file_name(file_name, subreddit)
    assert (post["author"], post["post_id"], post["score"]) == expected
    assert post["subreddit"] == subreddit


@pytest.mark.parametrize(
    ("file_name", "subreddit"),
    (("someone_otherSub_abc123_5.jpg", "pics"), ("notes.txt", "pics"), ("someone_pics_ab_5.jpg", "pics")),
)
def test_parse_file_name_rejects(file_name: str, subreddit: str):
    assert candidates.parse_file_name(file_name, subreddit) is None


def _post(
    post_id: str,
    author: str,
    subreddit: str = "pics",
    score: int = 1,
    posted_at: str = "2026-09-01 00:00:00",
    files: tuple[str, ...] = (),
) -> dict:
    return {
        "post_id": post_id,
        "author": author,
        "subreddit": subreddit,
        "score": score,
        "posted_at": posted_at,
        "files": list(files),
    }


def test_list_candidates_excludes_known_authors(conn: sqlite3.Connection):
    candidates.record_posts(
        conn,
        [
            _post("a1", "Newbie"),
            _post("a2", "newbie", "cats", score=5),
            _post("a3", "Followed"),
            _post("a4", "Paused"),
            _post("a5", "Spammer"),
            _post("a6", "Meh"),
            _post("a7", "DELETED"),
            _post("a8", "Quiet", posted_at="2020-01-01 00:00:00"),
        ],
    )
    assert candidates.record_posts(conn, [_post("a1", "Newbie")]) == 0
    service.add_source(conn, "user", "followed")
    service.add_source(conn, "user", "paused", status="paused")
    service.ignore_user(conn, "spammer")
    candidates.dismiss(conn, "meh", "not my thing")

    found = candidates.list_candidates(conn)
    assert [c["name"].lower() for c in found] == ["newbie", "quiet"]
    assert found[0]["posts"] == 2
    assert found[0]["subreddits"] == ["cats", "pics"]
    assert found[0]["total_score"] == 6
    assert [c["name"] for c in candidates.list_candidates(conn, min_posts=2)] == [found[0]["name"]]
    assert [c["name"] for c in candidates.list_candidates(conn, since="2026-01-01 00:00:00")] == [found[0]["name"]]
    assert [c["name"] for c in candidates.list_candidates(conn, subreddit="CATS")] == [found[0]["name"]]

    candidates.undismiss(conn, "Meh")
    assert "Meh" in [c["name"] for c in candidates.list_candidates(conn)]
    with pytest.raises(service.NotFoundError):
        candidates.undismiss(conn, "Meh")


def test_promote(conn: sqlite3.Connection):
    candidates.record_posts(conn, [_post("a1", "MixedCase"), _post("a2", "Sleepy")])
    candidates.dismiss(conn, "mixedcase")
    assert candidates.promote(conn, "u/mixedcase")["name"] == "MixedCase"
    assert candidates.list_dismissed(conn) == []

    service.add_source(conn, "user", "Sleepy", status="paused")
    assert candidates.promote(conn, "sleepy")["status"] == "active"
    assert candidates.list_candidates(conn) == []


def test_since_to_timestamp():
    assert candidates.since_to_timestamp("2026-01-31") == "2026-01-31 00:00:00"
    assert candidates.since_to_timestamp("30d") < candidates.since_to_timestamp("1d")
    with pytest.raises(service.ManagerError):
        candidates.since_to_timestamp("recently")


def test_scan_archive(conn: sqlite3.Connection, tmp_path: Path):
    root = tmp_path / "archive"
    files = {
        "subreddits/pics/alice_pics_aaa111_10.jpg": 1_700_000_000,
        "subreddits/pics/alice_pics_aaa111_10_2.jpg": 1_700_000_000,
        "subreddits/pics/bob_smith_pics_bbb222.png": 1_750_000_000,
        "subreddits/pics/random.txt": 1_750_000_000,
        "subreddits/old_sub/carol_old_sub_ccc333_3.mp4": 1_600_000_000,
        "users/dave/dave_pics_ddd444_1.jpg": 1_700_000_000,
    }
    for relative, mtime in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        os.utime(path, (mtime, mtime))

    messages = []
    totals = candidates.scan_archive(conn, root, progress=messages.append)
    assert totals == {"folders": 2, "files": 5, "new_posts": 3, "unparsed": 1}
    assert len(messages) == 2
    by_author = {c["name"]: c for c in candidates.list_candidates(conn)}
    assert set(by_author) == {"alice", "bob_smith", "carol"}
    assert by_author["alice"]["last_posted"] == "2023-11-14 22:13:20"
    assert by_author["carol"]["subreddits"] == ["old_sub"]

    assert candidates.scan_archive(conn, root)["new_posts"] == 0
    assert candidates.scan_archive(conn, root, subreddits=["PICS"])["folders"] == 1


def test_subreddit_run_records_posts(conn: sqlite3.Connection, fake_bdfrx: Path):
    use_fake(conn, fake_bdfrx)
    service.add_source(conn, "subreddit", "pics")
    service.add_source(conn, "user", "alice")
    results = {r["source_kind"]: r for r in runner.run(conn)}
    assert results["subreddit"]["posts_found"] == 1
    assert results["user"]["posts_found"] is None
    [candidate] = candidates.list_candidates(conn)
    assert (candidate["name"], candidate["posts"], candidate["subreddits"]) == ("Poster_One", 1, ["pics"])
    assert runner.run(conn, kinds=["subreddit"])[0]["posts_found"] == 0


def test_migrates_version_1_database(tmp_path: Path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db._SCHEMA_V1)
    old.execute("PRAGMA user_version = 1;")
    old.execute("INSERT INTO sources (kind, name) VALUES ('user', 'kept');")
    old.commit()
    old.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version;").fetchone()[0] == db.SCHEMA_VERSION
    assert service.get_source(conn, "user", "kept")
    assert "posts_found" in [row["name"] for row in conn.execute("PRAGMA table_info(runs);")]
    assert candidates.list_candidates(conn) == []


def test_cli_candidates(tmp_path: Path):
    cli_runner = CliRunner()
    db_args = ["--db", str(tmp_path / "cli.db")]
    root = tmp_path / "archive" / "subreddits" / "pics"
    root.mkdir(parents=True)
    for name in ("alice_pics_aaa111_10.jpg", "alice_pics_aaa222_3.jpg", "bob_pics_bbb111_1.jpg"):
        (root / name).write_text("x")

    def invoke(*args: str) -> str:
        result = cli_runner.invoke(cli, [*db_args, *args], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        return result.output

    assert "3 new post(s) recorded" in invoke("scan-archive", "--root", str(tmp_path / "archive"))
    listing = invoke("candidates")
    assert listing.index("alice") < listing.index("bob")
    assert "bob" not in invoke("candidates", "--min-posts", "2")
    invoke("candidates", "dismiss", "bob", "--reason", "meh")
    assert "bob" in invoke("candidates", "dismissed")
    assert "user alice is now active" in invoke("candidates", "promote", "alice")
    assert "alice" not in invoke("candidates")
    assert "active" in invoke("list", "--kind", "user")
    bad = cli_runner.invoke(cli, [*db_args, "candidates", "--since", "whenever"])
    assert bad.exit_code != 0


def test_parse_files_groups_files_by_post(tmp_path: Path):
    staging = tmp_path / "staging"
    paths = [staging / "a_pics_aaa111_5.jpg", staging / "a_pics_aaa111_5_2.jpg", staging / "b_pics_bbb222.mp4"]
    posts, unparsed = candidates.parse_files(
        [*paths, staging / "junk.txt"],
        "pics",
        final_path=lambda path: tmp_path / "archive" / path.name,
    )
    assert unparsed == 1
    by_id = {post["post_id"]: post for post in posts}
    assert by_id["aaa111"]["files"] == [
        str(tmp_path / "archive" / name) for name in ("a_pics_aaa111_5.jpg", "a_pics_aaa111_5_2.jpg")
    ]
    assert len(by_id["bbb222"]["files"]) == 1


def test_author_posts_and_file_paths(conn: sqlite3.Connection):
    candidates.record_posts(
        conn,
        [
            _post("old111", "alice", posted_at="2026-01-01 00:00:00", files=("/a/old.jpg",)),
            _post("new222", "alice", posted_at="2026-09-01 00:00:00", files=("/a/new_2.mp4", "/a/new_1.m4a")),
            _post("bob333", "bob", files=("/b/x.txt",)),
        ],
    )
    posts = candidates.author_posts(conn, "ALICE")
    assert [p["post_id"] for p in posts] == ["new222", "old111"]
    assert posts[0]["files"] == [
        {"index": 0, "name": "new_1.m4a", "kind": "audio"},
        {"index": 1, "name": "new_2.mp4", "kind": "video"},
    ]
    assert candidates.author_posts(conn, "alice", limit=1)[0]["post_id"] == "new222"
    assert candidates.post_file_path(conn, "new222", 1) == Path("/a/new_2.mp4")
    with pytest.raises(service.NotFoundError):
        candidates.post_file_path(conn, "new222", 2)
    with pytest.raises(service.NotFoundError):
        candidates.post_file_path(conn, "nosuch", 0)


@pytest.mark.parametrize(
    ("name", "kind"),
    (
        ("a.JPEG", "image"),
        ("a.gif", "image"),
        ("a.mp4", "video"),
        ("a.m4a", "audio"),
        ("a.txt", "text"),
        ("a", "other"),
    ),
)
def test_file_kind(name: str, kind: str):
    assert candidates.file_kind(name) == kind


def test_scan_fills_in_files_for_posts_recorded_without_them(conn: sqlite3.Connection, tmp_path: Path):
    candidates.record_posts(conn, [_post("aaa111", "alice")])
    folder = tmp_path / "archive" / "subreddits" / "pics"
    folder.mkdir(parents=True)
    (folder / "alice_pics_aaa111_10.jpg").write_text("x")
    totals = candidates.scan_archive(conn, tmp_path / "archive")
    assert totals["new_posts"] == 0
    assert candidates.post_file_path(conn, "aaa111", 0) == folder / "alice_pics_aaa111_10.jpg"


def test_staged_subreddit_run_records_final_paths(conn: sqlite3.Connection, fake_bdfrx: Path, tmp_path: Path):
    use_fake(conn, fake_bdfrx)
    service.set_setting(conn, "staging_root", str(tmp_path / "staging"))
    service.add_source(conn, "subreddit", "pics")
    runner.run(conn)
    path = candidates.post_file_path(conn, "p0st01", 0)
    assert path == tmp_path / "dest" / "subreddits" / "pics" / "Poster_One_pics_p0st01_12.jpg"
    assert path.exists()


# Setting aside ignored users' files


@pytest.fixture
def archive_with_alice(conn: sqlite3.Connection, tmp_path: Path) -> Path:
    root = tmp_path / "dest"
    names = {
        "subreddits/pics/alice_pics_aaa111_10.jpg": "a1",
        "subreddits/pics/alice_pics_aaa111_10_2.jpg": "a2",
        "subreddits/cats/alice_cats_bbb222_3.mp4": "b",
        "subreddits/pics/bob_pics_ccc333_1.jpg": "bob",
        "users/alice/alice_dogs_ddd444_7.jpg": "own",
    }
    for relative, content in names.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    candidates.scan_archive(conn, root)
    return root


def test_set_aside_and_restore(conn: sqlite3.Connection, archive_with_alice: Path):
    root = archive_with_alice
    aside = root / "ignored" / "alice"
    assert setaside.file_count(conn, "alice") == 4
    assert setaside.set_aside(conn, "u/alice", dry_run=True) == {"moved": 4, "skipped": []}
    assert (root / "subreddits/pics/alice_pics_aaa111_10.jpg").exists()

    assert setaside.set_aside(conn, "alice") == {"moved": 4, "skipped": []}
    assert sorted(str(p.relative_to(aside)) for p in aside.rglob("*") if p.is_file()) == [
        "subreddits/cats/alice_cats_bbb222_3.mp4",
        "subreddits/pics/alice_pics_aaa111_10.jpg",
        "subreddits/pics/alice_pics_aaa111_10_2.jpg",
        "users/alice/alice_dogs_ddd444_7.jpg",
    ]
    assert (aside / "subreddits/pics/alice_pics_aaa111_10_2.jpg").read_text() == "a2"
    assert (root / "subreddits/pics/bob_pics_ccc333_1.jpg").exists()
    assert not (root / "subreddits/pics/alice_pics_aaa111_10.jpg").exists()
    # Previews follow the files
    assert candidates.post_file_path(conn, "aaa111", 0) == aside / "subreddits/pics/alice_pics_aaa111_10.jpg"
    assert setaside.list_set_aside(conn)[0]["files"] == 4
    # Running again finds nothing left to move and reports nothing odd
    assert setaside.set_aside(conn, "alice") == {"moved": 0, "skipped": []}

    (aside / "subreddits/cats/alice_cats_bbb222_3.mp4").unlink()  # deleted during review
    summary = setaside.restore(conn, "alice")
    assert summary["moved"] == 3
    assert len(summary["skipped"]) == 1
    assert "deleted after review" in summary["skipped"][0]
    assert (root / "subreddits/pics/alice_pics_aaa111_10.jpg").read_text() == "a1"
    assert (root / "users/alice/alice_dogs_ddd444_7.jpg").read_text() == "own"
    assert candidates.post_file_path(conn, "aaa111", 0) == root / "subreddits/pics/alice_pics_aaa111_10.jpg"
    assert not aside.exists()
    assert setaside.list_set_aside(conn) == []


def test_set_aside_never_overwrites(conn: sqlite3.Connection, archive_with_alice: Path):
    root = archive_with_alice
    occupied = root / "ignored/alice/subreddits/pics/alice_pics_aaa111_10.jpg"
    occupied.parent.mkdir(parents=True)
    occupied.write_text("already here")
    (root / "subreddits/cats/alice_cats_bbb222_3.mp4").unlink()
    summary = setaside.set_aside(conn, "alice")
    assert summary["moved"] == 2
    assert sorted(reason.split(": ")[1].split(" ")[0] for reason in summary["skipped"]) == [
        str(occupied),
        "no",
    ]
    assert occupied.read_text() == "already here"
    assert (root / "subreddits/pics/alice_pics_aaa111_10.jpg").read_text() == "a1"


def test_restore_errors(conn: sqlite3.Connection, archive_with_alice: Path):
    with pytest.raises(service.NotFoundError):
        setaside.restore(conn, "alice")
    setaside.set_aside(conn, "alice")
    blocker = archive_with_alice / "subreddits/pics/alice_pics_aaa111_10.jpg"
    blocker.write_text("new download")
    summary = setaside.restore(conn, "alice")
    assert summary["moved"] == 3
    assert "already exists" in summary["skipped"][0]
    assert blocker.read_text() == "new download"


def test_ignored_folder_setting(conn: sqlite3.Connection, archive_with_alice: Path):
    for bad in ("../elsewhere", "users", "a/b", ""):
        with pytest.raises(service.ManagerError):
            service.set_setting(conn, "ignored_folder", bad)
    service.set_setting(conn, "ignored_folder", "_review")
    setaside.set_aside(conn, "alice")
    assert (archive_with_alice / "_review/alice/subreddits/pics/alice_pics_aaa111_10.jpg").exists()


def test_cli_ignore_set_aside(tmp_path: Path):
    cli_runner = CliRunner()
    db_args = ["--db", str(tmp_path / "cli.db")]
    root = tmp_path / "archive"
    folder = root / "subreddits" / "pics"
    folder.mkdir(parents=True)
    (folder / "alice_pics_aaa111_10.jpg").write_text("x")

    def invoke(*args: str) -> str:
        result = cli_runner.invoke(cli, [*db_args, *args], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        return result.output

    invoke("config", "set", "dest_root", str(root))
    invoke("scan-archive")
    assert "1 file(s) would move" in invoke("set-aside", "alice", "--dry-run")
    output = invoke("ignore", "alice", "--set-aside")
    assert "ignoring user alice" in output
    assert "1 file(s) moved" in output
    assert "alice" in invoke("set-aside")
    assert "1 file(s) restored" in invoke("restore", "alice")
    assert (folder / "alice_pics_aaa111_10.jpg").exists()


def test_ignored_users_go_in_a_file(conn: sqlite3.Connection, fake_bdfrx: Path, tmp_path: Path):
    use_fake(conn, fake_bdfrx)
    service.add_source(conn, "subreddit", "pics")
    for name in ("Spammer", "reposter", "bot_account"):
        service.ignore_user(conn, name)
    [result] = runner.run(conn)
    assert (tmp_path / "logs" / "ignore_users.txt").read_text().split() == ["bot_account", "reposter", "Spammer"]
    assert f"--ignore-user-file {tmp_path / 'logs' / 'ignore_users.txt'}" in result["command"]
    assert "--ignore-user " not in result["command"]


def test_build_command_rule_files(conn: sqlite3.Connection, tmp_path: Path):
    service.add_source(conn, "subreddit", "pics")
    source = service.get_source(conn, "subreddit", "pics")
    rules = {"ignore_user": ["a", "b"], "exclude_post": ["abc123"]}
    files = runner.rule_files(tmp_path, rules)
    assert files == {"exclude_post": tmp_path / "exclude_ids.txt", "ignore_user": tmp_path / "ignore_users.txt"}
    cmd = runner.build_command(service.get_settings(conn), source, rules, files)
    assert cmd[cmd.index("--ignore-user-file") + 1] == str(tmp_path / "ignore_users.txt")
    assert cmd[cmd.index("--exclude-id-file") + 1] == str(tmp_path / "exclude_ids.txt")
    assert "--ignore-user" not in cmd
    runner.write_rule_files(files, rules)
    assert (tmp_path / "ignore_users.txt").read_text() == "a\nb\n"


# Per-user history DBs


@pytest.mark.parametrize(
    ("file_name", "author", "expected"),
    (
        ("PaperDom_CumToAbuse_1f9ymfg_98.gif", "PaperDom", "1f9ymfg"),
        ("zoey_blowie_18_19_1wjikf4_51_2.mp4", "zoey_blowie", "1wjikf4"),
        ("alice_shibari_1wls15c_585.jpg", "alice", "1wls15c"),
        ("alice_pics_abc123.png", "ALICE", "abc123"),
        ("alice_gonewildaudio_1wnmh0b_15.txt", "alice", "1wnmh0b"),
        ("Thumbs.db", "alice", None),
    ),
)
def test_post_id_from_file_name(file_name: str, author: str, expected: str):
    assert history.post_id_from_file_name(file_name, author) == expected


def test_history_db_path_per_source(conn: sqlite3.Connection):
    service.add_source(conn, "user", "Alice")
    service.set_setting(conn, "history_db_user", "~/dbs/{name}.db")
    cmd = runner.build_command(service.get_settings(conn), service.get_source(conn, "user", "alice"), {})
    assert cmd[cmd.index("--db-file") + 1] == str(Path("~/dbs/Alice.db").expanduser())


def _shared_db(path: Path) -> Path:
    shared = sqlite3.connect(path)
    shared.executescript(
        "CREATE TABLE hash (hash TEXT UNIQUE, path TEXT); CREATE TABLE post_id (post_id TEXT UNIQUE);"
        "CREATE TABLE link (link TEXT UNIQUE);",
    )
    shared.executemany(
        "INSERT INTO hash VALUES (?, ?);",
        [
            ("h1", "/old/data_local/users/Alice/Alice_pics_aaa111_10.jpg"),
            ("h2", "/old/data_local/users/Alice/Alice_cats_aaa222_3_2.jpg"),
            ("h3", "/old/data_local/users/bob/bob_pics_bbb111.mp4"),
            ("h4", "/old/data_local/subreddits/pics/carol_pics_ccc111_1.jpg"),
        ],
    )
    shared.executemany(
        "INSERT INTO post_id VALUES (?);",
        [("aaa111",), ("aaa222",), ("aaa333",), ("bbb111",), ("zzz999",)],
    )
    shared.commit()
    shared.close()
    return path


def test_split_user_history(conn: sqlite3.Connection, tmp_path: Path):
    shared = _shared_db(tmp_path / "userdb")
    before = shared.read_bytes()
    archive = tmp_path / "archive"
    (archive / "users" / "alice").mkdir(parents=True)
    (archive / "users" / "alice" / "alice_dogs_aaa333_5.jpg").write_text("x")
    (archive / "users" / "alice" / "Thumbs.db").write_text("x")

    plan = history.plan_split(shared, [archive], known_names=["Alice", "dave"])
    assert plan.users["alice"].post_ids == {"aaa111", "aaa222", "aaa333"}
    assert set(plan.users["alice"].hashes) == {"h1", "h2"}
    assert plan.users["bob"].post_ids == {"bbb111"}
    assert "carol" not in plan.users
    assert (plan.shared_post_ids, plan.attributed_post_ids) == (5, 4)

    # Posts the manager saw in subreddits add to users already found, and only for post IDs the shared DB has
    candidates.record_posts(conn, [_post("zzz999", "BOB"), _post("yyy888", "bob"), _post("xxx777", "carol")])
    with_manager = history.plan_split(shared, [archive], known_names=["Alice"], manager_conn=conn)
    assert with_manager.users["bob"].post_ids == {"bbb111", "zzz999"}
    assert "carol" not in with_manager.users

    pattern = str(tmp_path / "userdbs" / "{name}.db")
    assert history.write_split(plan, pattern) == 2
    assert not (tmp_path / "userdbs" / "dave.db").exists()
    alice = sqlite3.connect(tmp_path / "userdbs" / "Alice.db")
    assert {r[0] for r in alice.execute("SELECT post_id FROM post_id;")} == {"aaa111", "aaa222", "aaa333"}
    assert alice.execute("SELECT path FROM hash WHERE hash = 'h1';").fetchone()[0].endswith("Alice_pics_aaa111_10.jpg")
    assert {r[0] for r in alice.execute("SELECT name FROM sqlite_master WHERE type = 'table';")} >= {"link"}
    alice.close()
    # Running again adds nothing new and keeps what is there
    assert history.write_split(plan, pattern) == 2
    assert (
        sqlite3.connect(tmp_path / "userdbs" / "Alice.db").execute("SELECT COUNT(*) FROM post_id;").fetchone()[0] == 3
    )
    assert shared.read_bytes() == before


def test_cli_split_user_history(tmp_path: Path):
    shared = _shared_db(tmp_path / "userdb")
    cli_runner = CliRunner()
    db_args = ["--db", str(tmp_path / "cli.db")]
    archive = tmp_path / "archive"
    archive.mkdir()
    pattern = str(tmp_path / "userdbs" / "{name}.db")

    def invoke(*args: str) -> str:
        result = cli_runner.invoke(cli, [*db_args, *args], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        return result.output

    invoke("add", "user", "alice", "newbie")
    dry = invoke("split-user-history", str(shared), "--into", pattern, "--archive-root", str(archive), "--dry-run")
    assert "2 user(s) with history; 3 of 5 shared post IDs (60.0%) attributed" in dry
    assert "newbie" in dry
    assert not (tmp_path / "userdbs").exists()
    out = invoke("split-user-history", str(shared), "--into", pattern, "--archive-root", str(archive))
    assert "2 per-user DB(s) written" in out
    assert (tmp_path / "userdbs" / "alice.db").exists()
    bad = cli_runner.invoke(cli, [*db_args, "split-user-history", str(shared), "--into", str(tmp_path / "x.db")])
    assert bad.exit_code != 0


# On-demand downloads (e.g. when following someone)


def test_time_override(conn: sqlite3.Connection):
    service.add_source(conn, "user", "alice", time="week")
    source = service.get_source(conn, "user", "alice")
    settings = service.get_settings(conn)
    assert (
        runner.build_command(settings, source, {})[runner.build_command(settings, source, {}).index("--time") + 1]
        == "week"
    )
    cmd = runner.build_command(settings, source, {}, time_override="all")
    assert cmd[cmd.index("--time") + 1] == "all"


def test_run_one(conn: sqlite3.Connection, fake_bdfrx: Path):
    use_fake(conn, fake_bdfrx)
    service.add_source(conn, "user", "alice")
    service.add_source(conn, "user", "sleepy", status="paused")
    run = runner.run_one(conn, "user", "ALICE", time_override="all")
    assert run["status"] == "ok"
    assert "--time all" in run["command"]
    with pytest.raises(service.ManagerError, match="paused"):
        runner.run_one(conn, "user", "sleepy")
    with pytest.raises(service.NotFoundError):
        runner.run_one(conn, "user", "nobody")
    service.ignore_user(conn, "alice")
    service.update_source(conn, "user", "alice", status="active")
    with pytest.raises(service.ManagerError, match="ignored"):
        runner.run_one(conn, "user", "alice")


def test_source_lock_keeps_a_source_from_running_twice(conn: sqlite3.Connection, fake_bdfrx: Path, tmp_path: Path):
    use_fake(conn, fake_bdfrx)
    service.add_source(conn, "user", "alice")
    service.add_source(conn, "user", "bob")
    alice = service.get_source(conn, "user", "alice")
    messages = []
    with runner.source_lock(tmp_path / "logs", alice):
        results = runner.run(conn, progress=messages.append)
        with pytest.raises(runner.RunLockedError, match="already being downloaded"):
            runner.run_one(conn, "user", "alice")
    assert [r["source_name"] for r in results] == ["bob"]
    assert any("already being downloaded" in m for m in messages)
    # The batch lock doesn't block single-source runs
    with runner.run_lock(tmp_path / "logs"):
        assert runner.run_one(conn, "user", "alice")["status"] == "ok"


def test_write_rule_files_leaves_no_temporary_files(tmp_path: Path):
    rules = {"ignore_user": ["a"], "exclude_post": ["abc123"]}
    runner.write_rule_files(runner.rule_files(tmp_path, rules), rules)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["exclude_ids.txt", "ignore_users.txt"]


def test_follow_download_time_setting(conn: sqlite3.Connection):
    assert service.get_settings(conn)["follow_download_time"] == "all"
    service.set_setting(conn, "follow_download_time", "month")
    with pytest.raises(service.ManagerError):
        service.set_setting(conn, "follow_download_time", "forever")


def test_cli_promote_download_and_run_time(tmp_path: Path, fake_bdfrx: Path):
    cli_runner = CliRunner()
    db_args = ["--db", str(tmp_path / "cli.db")]

    def invoke(*args: str) -> str:
        result = cli_runner.invoke(cli, [*db_args, *args], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        return result.output

    invoke("config", "set", "log_dir", str(tmp_path / "logs"))
    invoke("config", "set", "dest_root", str(tmp_path / "dest"))
    invoke("config", "set", "bdfrx_command", f"{sys.executable} {fake_bdfrx}")
    conn = db.connect(tmp_path / "cli.db")
    candidates.record_posts(conn, [_post("aaa111", "alice")])
    conn.close()
    out = invoke("candidates", "promote", "alice", "--download")
    assert "user alice is now active" in out
    assert "ok, 2 new file(s)" in out
    assert "--time all" in invoke("log", "1")
    assert "--time month" in invoke("run", "--dry-run", "--time", "month")


def test_default_bdfrx_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_python = tmp_path / "venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    monkeypatch.setattr(runner.sys, "executable", str(fake_python))
    assert runner.default_bdfrx_command() == [str(fake_python), "-m", "bdfrx"]
    (fake_python.parent / "bdfrx").write_text("#!/bin/sh\n")
    assert runner.default_bdfrx_command() == [str(fake_python.parent / "bdfrx")]


def test_bdfrx_runs_in_the_log_dir(
    conn: sqlite3.Connection,
    fake_bdfrx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # Wherever the manager itself is started from, e.g. a home directory holding a venv called bdfrx
    home = tmp_path / "home"
    (home / "bdfrx").mkdir(parents=True)
    monkeypatch.chdir(home)
    use_fake(conn, fake_bdfrx)
    service.add_source(conn, "user", "alice")
    run = runner.run_one(conn, "user", "alice")
    assert f"CWD {tmp_path / 'logs'}" in Path(run["log_path"]).read_text()


# Empty folders


def test_remove_empty_dirs(tmp_path: Path):
    (tmp_path / "empty" / "nested" / "deeper").mkdir(parents=True)
    runner.remove_empty_dirs(tmp_path / "empty")
    assert not (tmp_path / "empty").exists()
    (tmp_path / "full" / "nested").mkdir(parents=True)
    (tmp_path / "full" / "nested" / "keep.jpg").write_text("x")
    (tmp_path / "full" / "hollow").mkdir()
    runner.remove_empty_dirs(tmp_path / "full")
    assert (tmp_path / "full" / "nested" / "keep.jpg").exists()
    assert not (tmp_path / "full" / "hollow").exists()
    runner.remove_empty_dirs(tmp_path / "missing")  # no error


@pytest.mark.parametrize("staged", (False, True))
def test_runs_that_download_nothing_leave_no_folder(
    conn: sqlite3.Connection,
    fake_bdfrx: Path,
    tmp_path: Path,
    staged: bool,  # noqa: FBT001
):
    use_fake(conn, fake_bdfrx)
    if staged:
        service.set_setting(conn, "staging_root", str(tmp_path / "staging"))
    service.add_source(conn, "user", "gone")  # the fake reports the account as deleted and downloads nothing
    service.add_source(conn, "user", "alice")
    runner.run(conn)
    assert not (tmp_path / "dest" / "users" / "gone").exists()
    assert (tmp_path / "dest" / "users" / "alice" / "alice_post1.jpg").exists()
    if staged:
        assert not (tmp_path / "staging" / "users" / "gone").exists()
        assert not (tmp_path / "staging" / "users" / "alice").exists()


def test_set_aside_removes_emptied_user_folder(conn: sqlite3.Connection, archive_with_alice: Path):
    setaside.set_aside(conn, "alice")
    assert not (archive_with_alice / "users" / "alice").exists()
    assert (archive_with_alice / "subreddits" / "pics").is_dir()  # shared, and bob's file is still there
    setaside.restore(conn, "alice")
    assert (archive_with_alice / "users" / "alice" / "alice_dogs_ddd444_7.jpg").exists()
