import os
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdfrx.manager import candidates, db, importer, runner, service
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
    user_cmd = runner.build_command(settings, service.get_source(conn, "user", "alice"), rules, Path("ids.txt"))
    assert user_cmd[3:6] == ["download", str(Path(settings["dest_root"], "users", "alice")), "--user"]
    assert "--submitted" in user_cmd
    assert "--filter-gender" not in user_cmd
    assert user_cmd[user_cmd.index("--min-score") + 1] == "3"
    assert user_cmd[user_cmd.index("--db-file") + 1].endswith("userdb/userdb")
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
    assert "--ignore-user bob" in invoke("run", "--dry-run")
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


def _post(post_id: str, author: str, subreddit: str = "pics", score: int = 1, posted_at: str = "2026-09-01 00:00:00"):
    return {"post_id": post_id, "author": author, "subreddit": subreddit, "score": score, "posted_at": posted_at}


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
