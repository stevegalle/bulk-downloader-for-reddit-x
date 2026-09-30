import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import click

from bdfrx.manager import candidates, db, history, importer, runner, service, setaside

KIND = click.Choice(service.SOURCE_KINDS)
CLEAR = "default"


def _conn(context: click.Context) -> sqlite3.Connection:
    return context.obj["conn"]


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def _override(value: Optional[str], *, as_int: bool = False) -> Any:
    """Map a CLI override value to what update_source expects; 'default' clears the override"""
    if value is None or value == CLEAR:
        return None
    if as_int:
        try:
            return int(value)
        except ValueError:
            raise click.BadParameter(f"{value!r} is not a whole number (or '{CLEAR}')")
    return value


def _override_fields(**options: Optional[str]) -> dict:
    int_fields = {"min_score", "post_limit"}
    return {
        field: _override(value, as_int=field in int_fields) for field, value in options.items() if value is not None
    }


def _format_source(source: dict) -> str:
    overrides = [f"{field}={source[field]}" for field in service.SOURCE_OVERRIDES if source[field] is not None]
    last = f"last {source['last_run_at']} {source['last_status']}" if source["last_run_at"] else "never run"
    parts = [f"{source['kind']:<9} {source['name']:<24} {source['status']:<7} {last}"]
    if overrides:
        parts.append(" ".join(overrides))
    if source["note"]:
        parts.append(f"# {source['note']}")
    return "  ".join(parts)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False),
    default=None,
    envvar="BDFRX_MANAGER_DB",
    help="Manager database (default: ~/.config/bdfrx/manager.db, or $BDFRX_MANAGER_DB)",
)
@click.pass_context
def cli(context: click.Context, db_path: Optional[str]) -> None:
    """Manage which users and subreddits bdfrx downloads, and run it on a schedule."""
    context.obj = {"conn": db.connect(db_path), "db_path": db_path}


# Sources


def _source_options(func):  # noqa: ANN001, ANN202
    options = [
        click.option("--sort", type=click.Choice((*service.SORTS, CLEAR)), help="Sort order override"),
        click.option("--time", type=click.Choice((*service.TIMES, CLEAR)), help="Time filter override"),
        click.option(
            "--gender",
            "filter_gender",
            type=click.Choice((*service.GENDERS, CLEAR)),
            help="Gender filter override ('none' turns it off for this source)",
        ),
        click.option("--min-score", help=f"Minimum score override ('{CLEAR}' to clear)"),
        click.option("--limit", "post_limit", help=f"Maximum posts per run ('{CLEAR}' to clear)"),
        click.option("--extra-args", help="Extra bdfrx arguments, as one quoted string"),
        click.option("--note", help="Free-text note"),
    ]
    for option in reversed(options):
        func = option(func)
    return func


@cli.command("add")
@click.argument("kind", type=KIND)
@click.argument("names", nargs=-1, required=True)
@click.option("--paused", is_flag=True, help="Add without scheduling downloads yet")
@_source_options
@click.pass_context
def cli_add(context: click.Context, kind: str, names: tuple[str], paused: bool, **options: Optional[str]) -> None:
    """Add users or subreddits to download."""
    fields = _override_fields(**options)
    if paused:
        fields["status"] = "paused"
    for name in names:
        try:
            source = service.add_source(_conn(context), kind, name, **fields)
            click.echo(f"added {kind} {source['name']}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli.command("set")
@click.argument("kind", type=KIND)
@click.argument("name")
@_source_options
@click.pass_context
def cli_set(context: click.Context, kind: str, name: str, **options: Optional[str]) -> None:
    """Change a source's settings. Use 'default' to go back to the global default."""
    fields = _override_fields(**options)
    if not fields:
        raise click.UsageError("Nothing to change; pass at least one option")
    try:
        click.echo(_format_source(service.update_source(_conn(context), kind, name, **fields)))
    except service.ManagerError as e:
        raise click.ClickException(str(e))


def _set_status(context: click.Context, kind: str, names: tuple[str], status: str) -> None:
    for name in names:
        try:
            source = service.update_source(_conn(context), kind, name, status=status)
            click.echo(f"{kind} {source['name']} is now {status}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli.command("pause")
@click.argument("kind", type=KIND)
@click.argument("names", nargs=-1, required=True)
@click.pass_context
def cli_pause(context: click.Context, kind: str, names: tuple[str]) -> None:
    """Stop downloading sources, keeping them in the list."""
    _set_status(context, kind, names, "paused")


@cli.command("resume")
@click.argument("kind", type=KIND)
@click.argument("names", nargs=-1, required=True)
@click.pass_context
def cli_resume(context: click.Context, kind: str, names: tuple[str]) -> None:
    """Start downloading paused or banned sources again."""
    _set_status(context, kind, names, "active")


@cli.command("ban")
@click.argument("kind", type=KIND)
@click.argument("names", nargs=-1, required=True)
@click.pass_context
def cli_ban(context: click.Context, kind: str, names: tuple[str]) -> None:
    """Mark sources as banned or gone from Reddit (not downloaded)."""
    _set_status(context, kind, names, "banned")


@cli.command("remove")
@click.argument("kind", type=KIND)
@click.argument("names", nargs=-1, required=True)
@click.pass_context
def cli_remove(context: click.Context, kind: str, names: tuple[str]) -> None:
    """Delete sources from the list. Their run history is kept."""
    for name in names:
        try:
            service.remove_source(_conn(context), kind, name)
            click.echo(f"removed {kind} {name}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli.command("list")
@click.option("--kind", type=KIND)
@click.option("--status", type=click.Choice(service.SOURCE_STATUSES))
@click.option("--search", help="Only names containing this text")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def cli_list(
    context: click.Context,
    kind: Optional[str],
    status: Optional[str],
    search: Optional[str],
    as_json: bool,
) -> None:
    """List sources."""
    sources = service.list_sources(_conn(context), kind, status, search)
    if as_json:
        _echo_json(sources)
        return
    for source in sources:
        click.echo(_format_source(source))
    click.echo(f"{len(sources)} source(s)", err=True)


@cli.command("show")
@click.argument("kind", type=KIND)
@click.argument("name")
@click.pass_context
def cli_show(context: click.Context, kind: str, name: str) -> None:
    """Show one source, the options it will run with, and its recent runs."""
    conn = _conn(context)
    try:
        source = service.get_source(conn, kind, name)
    except service.ManagerError as e:
        raise click.ClickException(str(e))
    settings = service.get_settings(conn)
    _echo_json(
        {
            "source": source,
            "effective_options": runner.effective_options(settings, source),
            "destination": str(runner.destination(settings, source)),
            "recent_runs": [
                {key: run[key] for key in ("id", "started_at", "status", "exit_code", "files_new")}
                for run in service.list_runs(conn, limit=5, kind=kind, name=source["name"])
            ],
        },
    )


@cli.command("import-dirs")
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Show what would be imported without changing anything")
@click.pass_context
def cli_import_dirs(context: click.Context, root: Path, dry_run: bool) -> None:
    """Import sources from the old data_local directory layout.

    \b
    ROOT/users/<name>        -> active user
    ROOT/subreddits/<name>   -> active subreddit
    ROOT/users_backup/<name> -> paused user, unless also in ROOT/users
    ROOT/banned/<name>       -> banned user, wherever else it appears
    """
    report = importer.import_directories(_conn(context), root, dry_run=dry_run)
    verb = "would add" if dry_run else "added"
    for label in report["added"]:
        click.echo(f"{verb} {label}")
    for label in report["invalid"]:
        click.echo(f"invalid name, not imported: {label}", err=True)
    click.echo(
        f"{len(report['added'])} {verb}, {len(report['existing'])} already present, {len(report['invalid'])} invalid",
        err=True,
    )


# Rules


@cli.command("ignore")
@click.argument("users", nargs=-1, required=True)
@click.option("--reason")
@click.option(
    "--set-aside",
    "move_files",
    is_flag=True,
    help="Also move their downloaded files into the ignored folder",
)
@click.pass_context
def cli_ignore(context: click.Context, users: tuple[str], reason: Optional[str], move_files: bool) -> None:
    """Never download posts by these users, in any run. Pauses them if they are sources."""
    for user in users:
        try:
            rule = service.ignore_user(_conn(context), user, reason)
            click.echo(f"ignoring user {rule['value']}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)
            continue
        if move_files:
            _echo_set_aside(rule["value"], setaside.set_aside(_conn(context), rule["value"]), "moved")


def _echo_set_aside(name: str, summary: setaside.Summary, verb: str) -> None:
    click.echo(f"{name}: {summary['moved']} file(s) {verb}, {len(summary['skipped'])} skipped")
    for reason in summary["skipped"]:
        click.echo(f"    skipped {reason}", err=True)


@cli.command("set-aside")
@click.argument("users", nargs=-1)
@click.option("--dry-run", is_flag=True, help="Count what would move without moving anything")
@click.pass_context
def cli_set_aside(context: click.Context, users: tuple[str], dry_run: bool) -> None:
    """Move users' downloaded files into <archive>/<ignored_folder>/<user>/ for review. Nothing is deleted.

    With no USERS, lists users whose files are currently set aside.
    """
    conn = _conn(context)
    if not users:
        entries = setaside.list_set_aside(conn)
        for entry in entries:
            click.echo(f"{entry['author']:<24} {entry['files']:>6} file(s)  since {entry['first_moved'][:10]}")
        click.echo(f"{len(entries)} user(s) with files set aside", err=True)
        return
    for user in users:
        try:
            _echo_set_aside(user, setaside.set_aside(conn, user, dry_run=dry_run), "would move" if dry_run else "moved")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli.command("restore")
@click.argument("users", nargs=-1, required=True)
@click.pass_context
def cli_restore(context: click.Context, users: tuple[str]) -> None:
    """Move users' set-aside files back where they were. Does not unignore them."""
    for user in users:
        try:
            _echo_set_aside(user, setaside.restore(_conn(context), user), "restored")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli.command("unignore")
@click.argument("users", nargs=-1, required=True)
@click.pass_context
def cli_unignore(context: click.Context, users: tuple[str]) -> None:
    """Stop ignoring users. Does not resume them as sources; use 'resume' for that."""
    for user in users:
        try:
            service.remove_rule(_conn(context), "ignore_user", user)
            click.echo(f"no longer ignoring {user}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli.command("exclude")
@click.argument("posts", nargs=-1, required=True)
@click.option("--reason")
@click.pass_context
def cli_exclude(context: click.Context, posts: tuple[str], reason: Optional[str]) -> None:
    """Never download these posts. Accepts post IDs or Reddit links."""
    for post in posts:
        try:
            rule = service.add_rule(_conn(context), "exclude_post", post, reason)
            click.echo(f"excluding post {rule['value']}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli.group("rule")
def cli_rule() -> None:
    """Manage download rules (ignore_user, exclude_post, skip_subreddit, skip_domain)."""


@cli_rule.command("list")
@click.option("--kind", type=click.Choice(service.RULE_KINDS))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def cli_rule_list(context: click.Context, kind: Optional[str], as_json: bool) -> None:
    rules = service.list_rules(_conn(context), kind)
    if as_json:
        _echo_json(rules)
        return
    for rule in rules:
        reason = f"  # {rule['reason']}" if rule["reason"] else ""
        click.echo(f"{rule['kind']:<15} {rule['value']}{reason}")
    click.echo(f"{len(rules)} rule(s)", err=True)


@cli_rule.command("add")
@click.argument("kind", type=click.Choice(service.RULE_KINDS))
@click.argument("values", nargs=-1, required=True)
@click.option("--reason")
@click.pass_context
def cli_rule_add(context: click.Context, kind: str, values: tuple[str], reason: Optional[str]) -> None:
    for value in values:
        try:
            rule = service.add_rule(_conn(context), kind, value, reason)
            click.echo(f"added {kind} {rule['value']}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli_rule.command("remove")
@click.argument("kind", type=click.Choice(service.RULE_KINDS))
@click.argument("values", nargs=-1, required=True)
@click.pass_context
def cli_rule_remove(context: click.Context, kind: str, values: tuple[str]) -> None:
    for value in values:
        try:
            service.remove_rule(_conn(context), kind, value)
            click.echo(f"removed {kind} {value}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


# Settings


@cli.group("config")
def cli_config() -> None:
    """View and change global settings."""


@cli_config.command("list")
@click.pass_context
def cli_config_list(context: click.Context) -> None:
    settings = service.get_settings(_conn(context))
    for key, value in settings.items():
        marker = "" if value == service.DEFAULT_SETTINGS[key] else "  (changed)"
        click.echo(f"{key} = {value}{marker}")
        click.echo(f"    {service.SETTING_HELP[key]}")


@cli_config.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def cli_config_set(context: click.Context, key: str, value: str) -> None:
    try:
        service.set_setting(_conn(context), key, value)
    except service.ManagerError as e:
        raise click.ClickException(str(e))
    click.echo(f"{key} = {value}")


@cli_config.command("unset")
@click.argument("key")
@click.pass_context
def cli_config_unset(context: click.Context, key: str) -> None:
    """Restore a setting to its default."""
    try:
        service.set_setting(_conn(context), key, None)
    except service.ManagerError as e:
        raise click.ClickException(str(e))
    click.echo(f"{key} = {service.DEFAULT_SETTINGS[key]} (default)")


# Candidates


@cli.group("candidates", invoke_without_command=True)
@click.option("--min-posts", default=1, show_default=True, help="Only authors with at least this many posts")
@click.option("--since", help="Only authors who posted since then: 30d, 12w, 6m, or a date like 2026-01-31")
@click.option("--subreddit", help="Only authors who posted in this subreddit")
@click.option("-n", "--limit", type=int, default=50, show_default=True, help="Show at most this many (0 for all)")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def cli_candidates(
    context: click.Context,
    min_posts: int,
    since: Optional[str],
    subreddit: Optional[str],
    limit: int,
    as_json: bool,
) -> None:
    """Users seen posting in your subreddits who you don't follow, ignore, or dismissed. Most active first.

    \b
    Posts are recorded by every subreddit run. To include what is already in the archive, run 'scan-archive' once.
    """
    if context.invoked_subcommand:
        return
    try:
        since_ts = candidates.since_to_timestamp(since) if since else None
        if subreddit:
            subreddit = service.normalize_name("subreddit", subreddit)
    except service.ManagerError as e:
        raise click.ClickException(str(e))
    found = candidates.list_candidates(_conn(context), min_posts, since_ts, subreddit, limit or None)
    if as_json:
        _echo_json(found)
        return
    for candidate in found:
        subs = ", ".join(candidate["subreddits"][:4])
        if len(candidate["subreddits"]) > 4:
            subs += f" +{len(candidate['subreddits']) - 4} more"
        click.echo(
            f"{candidate['name']:<24} {candidate['posts']:>4} posts  score {candidate['total_score'] or 0:>6}  "
            f"last {(candidate['last_posted'] or '?')[:10]}  in {subs}",
        )
    click.echo(f"{len(found)} candidate(s)", err=True)


@cli_candidates.command("promote")
@click.argument("names", nargs=-1, required=True)
@click.option("--download", is_flag=True, help="Download their posts right away (time window: follow_download_time)")
@click.pass_context
def cli_candidates_promote(context: click.Context, names: tuple[str], download: bool) -> None:
    """Start downloading these users (adds them as active user sources)."""
    conn = _conn(context)
    for name in names:
        try:
            source = candidates.promote(conn, name)
            click.echo(f"user {source['name']} is now active")
            if download:
                time = service.get_settings(conn)["follow_download_time"]
                click.echo(f"    downloading their posts (time: {time})…")
                run = runner.run_one(conn, "user", source["name"], time_override=time)
                click.echo(f"    {run['status']}, {run['files_new']} new file(s) (run {run['id']})")
                if run["deactivated"]:
                    click.echo(f"    deactivated: {run['deactivated']}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli_candidates.command("dismiss")
@click.argument("names", nargs=-1, required=True)
@click.option("--reason")
@click.pass_context
def cli_candidates_dismiss(context: click.Context, names: tuple[str], reason: Optional[str]) -> None:
    """Hide users from the candidate list. Their posts still download; use 'ignore' to stop that."""
    for name in names:
        try:
            candidates.dismiss(_conn(context), name, reason)
            click.echo(f"dismissed {name}")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli_candidates.command("undismiss")
@click.argument("names", nargs=-1, required=True)
@click.pass_context
def cli_candidates_undismiss(context: click.Context, names: tuple[str]) -> None:
    """Show dismissed users in the candidate list again."""
    for name in names:
        try:
            candidates.undismiss(_conn(context), name)
            click.echo(f"{name} is a candidate again")
        except service.ManagerError as e:
            click.echo(f"skipped: {e}", err=True)


@cli_candidates.command("dismissed")
@click.pass_context
def cli_candidates_dismissed(context: click.Context) -> None:
    """List dismissed users."""
    dismissed = candidates.list_dismissed(_conn(context))
    for entry in dismissed:
        reason = f"  # {entry['reason']}" if entry["reason"] else ""
        click.echo(f"{entry['name']:<24} {entry['dismissed_at'][:10]}{reason}")
    click.echo(f"{len(dismissed)} dismissed", err=True)


@cli.command("scan-archive")
@click.option("--subreddit", "subreddits", multiple=True, help="Only this subreddit folder (repeatable)")
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Archive root to scan instead of the dest_root setting",
)
@click.pass_context
def cli_scan_archive(context: click.Context, subreddits: tuple[str], root: Optional[Path]) -> None:
    """Record posts from files already in the archive's subreddit folders, for the candidate list.

    Reads file names and dates only; nothing is downloaded or changed. Posts already recorded are skipped,
    so it is safe to run again or to stop part way.
    """
    conn = _conn(context)
    root = root or Path(service.get_settings(conn)["dest_root"]).expanduser()
    if not (root / "subreddits").is_dir():
        raise click.ClickException(f"No subreddits folder in {root}")
    totals = candidates.scan_archive(conn, root, list(subreddits) or None, progress=click.echo)
    click.echo(
        f"{totals['folders']} folder(s), {totals['files']} file(s), {totals['new_posts']} new post(s) recorded, "
        f"{totals['unparsed']} file name(s) not understood",
    )


@cli.command("split-user-history")
@click.argument("shared_db", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--into",
    "pattern",
    required=True,
    help="Per-user DB path containing {name}, e.g. ~/.config/bdfrx/userdbs/{name}.db",
)
@click.option(
    "--archive-root",
    "archive_roots",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Archive whose users/<name>/ folders to read post IDs from (repeatable; default: dest_root)",
)
@click.option("--dry-run", is_flag=True, help="Report what each user would get without writing anything")
@click.pass_context
def cli_split_user_history(
    context: click.Context,
    shared_db: Path,
    pattern: str,
    archive_roots: tuple[Path],
    dry_run: bool,
) -> None:
    """Split a shared bdfrx history DB (like userdb) into one DB per user.

    The shared DB is only read. Existing per-user DBs are added to, never replaced. Afterwards, point
    history_db_user at the same pattern to use them.
    """
    if "{name}" not in pattern:
        raise click.BadParameter("must contain {name}", param_hint="--into")
    conn = _conn(context)
    roots = list(archive_roots) or [Path(service.get_settings(conn)["dest_root"]).expanduser()]
    names = [source["name"] for source in service.list_sources(conn, kind="user")]
    plan = history.plan_split(shared_db, roots, names, progress=click.echo, manager_conn=conn)
    with_rows = [u for u in plan.users.values() if u.post_ids or u.hashes]
    share = 100 * plan.attributed_post_ids / plan.shared_post_ids if plan.shared_post_ids else 0
    click.echo(
        f"{len(with_rows)} user(s) with history; {plan.attributed_post_ids} of {plan.shared_post_ids} shared post IDs "
        f"({share:.1f}%) attributed, {sum(len(u.post_ids) for u in with_rows)} post IDs and "
        f"{sum(len(u.hashes) for u in with_rows)} hashes in total",
    )
    empty = sorted(u.name for u in plan.users.values() if not (u.post_ids or u.hashes))
    if empty:
        click.echo(
            f"{len(empty)} user source(s) with no history found (they'll start fresh): {', '.join(empty[:10])}"
            + (" …" if len(empty) > 10 else ""),
        )
    if dry_run:
        for user in sorted(with_rows, key=lambda u: u.name.lower())[:15]:
            click.echo(f"  would write {history.user_db_path(pattern, user.name)}: {len(user.post_ids)} post IDs")
        return
    written = history.write_split(plan, pattern)
    click.echo(f"{written} per-user DB(s) written. To use them: bdfrx-manager config set history_db_user '{pattern}'")


# Running


@cli.command("run")
@click.option("--kind", "kinds", type=KIND, multiple=True, help="Only this kind of source (repeatable)")
@click.option(
    "--name",
    "names",
    multiple=True,
    help="Only this source (repeatable). A bare name matches both kinds; u/name or r/name matches just one",
)
@click.option("--sort", type=click.Choice(service.SORTS), help="Override every source's sort for this run")
@click.option("--time", type=click.Choice(service.TIMES), help="Override every source's time filter for this run")
@click.option("--dry-run", is_flag=True, help="Print the bdfrx commands without running them")
@click.pass_context
def cli_run(
    context: click.Context,
    kinds: tuple[str],
    names: tuple[str],
    sort: Optional[str],
    time: Optional[str],
    dry_run: bool,
) -> None:
    """Download new posts for every active source. This is what cron should call."""
    try:
        results = runner.run(
            _conn(context),
            kinds=list(kinds) or None,
            names=list(names) or None,
            sort_override=sort,
            time_override=time,
            dry_run=dry_run,
            progress=click.echo,
        )
    except runner.RunLockedError as e:
        click.echo(str(e), err=True)
        sys.exit(75)  # EX_TEMPFAIL: cron will try again next time
    if dry_run:
        for plan in results:
            if plan["skipped"]:
                click.echo(f"# {plan['kind']} {plan['name']}: skipped, {plan['skipped']}")
            else:
                click.echo(plan["command"])
        return
    failed = [r for r in results if r["status"] != "ok"]
    new_files = sum(r["files_new"] or 0 for r in results)
    click.echo(f"{len(results)} source(s) run, {len(failed)} failed, {new_files} new file(s)")
    if failed:
        sys.exit(1)


@cli.command("runs")
@click.option("-n", "--limit", default=20, show_default=True)
@click.option("--kind", type=KIND)
@click.option("--name")
@click.option("--failed", is_flag=True, help="Only failed runs")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def cli_runs(
    context: click.Context,
    limit: int,
    kind: Optional[str],
    name: Optional[str],
    failed: bool,
    as_json: bool,
) -> None:
    """Show recent runs."""
    runs = service.list_runs(_conn(context), limit, kind, name, "failed" if failed else None)
    if as_json:
        _echo_json(runs)
        return
    for run in runs:
        click.echo(
            f"{run['id']:>6}  {run['started_at']}  {run['status']:<7} {run['source_kind']:<9} "
            f"{run['source_name']:<24} {run['files_new'] if run['files_new'] is not None else '-':>4} new",
        )


@cli.command("log")
@click.argument("run_id", type=int)
@click.pass_context
def cli_log(context: click.Context, run_id: int) -> None:
    """Print the full bdfrx output of a run."""
    try:
        run = service.get_run(_conn(context), run_id)
    except service.ManagerError as e:
        raise click.ClickException(str(e))
    click.echo(f"$ {run['command']}", err=True)
    log_path = Path(run["log_path"]) if run["log_path"] else None
    if log_path and log_path.exists():
        click.echo(log_path.read_text(errors="replace"))
    else:
        click.echo("(log file is gone; last lines stored in the database:)", err=True)
        click.echo(run["output_tail"] or "")


@cli.command("status")
@click.pass_context
def cli_status(context: click.Context) -> None:
    """Summarize sources, rules and the last run."""
    _echo_json(service.summary(_conn(context)))


@cli.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True, help="0.0.0.0 to accept connections from the network")
@click.option("--port", default=8765, show_default=True)
@click.option(
    "--api-key",
    envvar="BDFRX_MANAGER_API_KEY",
    help="Require this key in an X-API-Key header on every request (or set $BDFRX_MANAGER_API_KEY)",
)
@click.pass_context
def cli_serve(context: click.Context, host: str, port: int, api_key: Optional[str]) -> None:
    """Serve the HTTP API (needs the 'manager' extra: pip install 'bdfrx[manager]').

    Settings that name commands or paths, and per-source extra_args, are read-only over the API;
    change them with 'bdfrx-manager config set' on the server.
    """
    try:
        import uvicorn

        from bdfrx.manager.api import create_app
    except ImportError as e:
        raise click.ClickException(f"{e}. Install the API dependencies with: pip install 'bdfrx[manager]'")
    if host not in ("127.0.0.1", "localhost", "::1") and not api_key:
        click.echo(
            f"Warning: serving on {host} without an API key; anyone who can reach this port can use it",
            err=True,
        )
    uvicorn.run(create_app(context.obj["db_path"], api_key), host=host, port=port)


if __name__ == "__main__":
    cli()
