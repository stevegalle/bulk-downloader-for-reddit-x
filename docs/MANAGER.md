# bdfrx-manager

`bdfrx-manager` keeps a list of users and subreddits to download in a SQLite database, and runs bdfrx for each of
them on a schedule. It replaces the old `data_local/{users,subreddits}/<name>` directory layout and the `update.sh`
wrapper scripts.

## Install

```bash
pip install -e '.[manager]'   # the [manager] extra adds FastAPI/uvicorn for the HTTP API; the CLI works without it
```

The database lives at `~/.config/bdfrx/manager.db` unless `--db` or `$BDFRX_MANAGER_DB` says otherwise.

## Moving over from data_local

```bash
bdfrx-manager import-dirs ~/bdfrx/data_local --dry-run
bdfrx-manager import-dirs ~/bdfrx/data_local
```

| Directory                 | Imported as                                        |
|---------------------------|----------------------------------------------------|
| `users/<name>`            | active user                                        |
| `subreddits/<name>`       | active subreddit                                   |
| `users_backup/<name>`     | paused user, unless the name is also in `users/`   |
| `banned/<name>`           | banned user, wherever else the name appears        |

Importing again only adds names that are not already in the database.

## Settings

`bdfrx-manager config list` shows every setting with its meaning. The defaults reproduce `update.sh`:

| Setting                  | Default                                      |
|--------------------------|----------------------------------------------|
| `dest_root`              | `/mnt/c/bdfrx`                               |
| `staging_root`           | empty: download straight into `dest_root`    |
| `default_sort`           | `new`                                        |
| `default_time`           | `day`                                        |
| `default_filter_gender`  | `male`                                       |
| `history_db_user`        | `~/.config/bdfrx/userdb/userdb`              |
| `history_db_subreddit`   | empty: bdfrx's default database              |

Set `staging_root` when `dest_root` is a network share: files are downloaded locally, then moved into the archive
without overwriting anything already there. It is required when the share does not let you set file timestamps
(for example a CIFS mount where files are owned by root), because bdfrx stops with "Operation not permitted" when it
sets a downloaded file's date. When the manager runs on the machine that holds the archive, leave it
empty.

## Everyday use

```bash
bdfrx-manager add user some_user other_user
bdfrx-manager add subreddit somesub --sort top --time week
bdfrx-manager set user some_user --min-score 20      # per-source override
bdfrx-manager set user some_user --sort default      # back to the global default
bdfrx-manager pause user some_user                   # stop downloading, keep in list
bdfrx-manager resume user some_user
bdfrx-manager list --kind user --status active
bdfrx-manager show user some_user                    # overrides, effective options, recent runs
```

### Rules

Rules apply to every run, not just one source. Ignored users and excluded posts are written to `ignore_users.txt` and
`exclude_ids.txt` in `log_dir` before each run and passed to bdfrx as `--ignore-user-file` / `--exclude-id-file`, so the
lists can grow to any length without growing the command line. Ignored users match regardless of capitalization.

```bash
bdfrx-manager ignore some_user --reason "reposts"    # never download their posts anywhere; pauses them as a source
bdfrx-manager exclude https://www.reddit.com/r/x/comments/abc123/...   # never download this post
bdfrx-manager rule add skip_subreddit somesub        # skip posts in this subreddit (also skips it as a source)
bdfrx-manager rule add skip_domain example.com
bdfrx-manager rule list
bdfrx-manager rule remove skip_domain example.com
```

## Candidate users

Every subreddit run records who posted what it downloaded, read from the downloaded file names
(`{REDDITOR}_{SUBREDDIT}_{POSTID}_{UPVOTES}`, or the older form without `_{UPVOTES}`). Nothing extra is asked of
Reddit. File modification times give when they posted: bdfrx sets them to the post date, but a share that doesn't
let you set timestamps (such as a CIFS mount owned by root) keeps the copy date instead, so dates from
`scan-archive` on such a share are when files arrived, not when they were posted.

A **candidate** is an author seen that way who is not a user source (in any status), not ignored and not dismissed.

```bash
bdfrx-manager scan-archive                     # one-time: record posts already in <dest_root>/subreddits
bdfrx-manager candidates                       # most active first
bdfrx-manager candidates --min-posts 5 --since 30d --subreddit somesub
bdfrx-manager candidates promote someone       # add as an active user source
bdfrx-manager candidates dismiss someone       # hide from the list; their posts still download
bdfrx-manager candidates undismiss someone
bdfrx-manager candidates dismissed
```

Each post's file locations are recorded too, so the API can serve them as previews; it only ever serves files it
has recorded. `scan-archive` fills in locations for posts recorded before they were kept.

`scan-archive` only reads file names and dates. It skips posts already recorded, so it can be stopped and run again,
and `--subreddit` limits it to some folders. Over the network it takes about a minute per 45,000 files.

## Setting aside an ignored user's files

Ignoring someone stops new downloads but leaves their files where they are. To move them out of the way for review,
without deleting anything:

```bash
bdfrx-manager ignore someone --set-aside       # ignore and move their files in one step
bdfrx-manager set-aside someone --dry-run      # count what would move
bdfrx-manager set-aside someone                # move (does not ignore them)
bdfrx-manager set-aside                        # list users with files set aside
bdfrx-manager restore someone                  # move everything back (does not unignore them)
```

Files move to `<archive>/<ignored_folder>/<user>/`, keeping their `subreddits/<name>/` or `users/<name>/` path, where
`<archive>` is the folder holding the file's `subreddits/` folder and `ignored_folder` defaults to `ignored`. Moves
stay on the same filesystem, so they are renames: instant, and dates are kept. Nothing is ever overwritten: a file
whose target already exists is skipped and reported. Every move is recorded, so `restore` puts files back exactly;
files you deleted during review are noted and no longer tracked. Previews keep working while files are set aside.

It moves the files the manager knows about: every post recorded by runs or `scan-archive`, plus the whole
`users/<user>/` folder if they were followed. In the web UI, **Ignore** offers the same move as a checkbox, and its
**Undo** restores the files too.

## Running

```bash
bdfrx-manager run --dry-run           # print the bdfrx commands it would run
bdfrx-manager run                     # every active source, least recently run first
bdfrx-manager run --kind user --sort top
bdfrx-manager run --name some_user
bdfrx-manager runs --failed           # recent failures
bdfrx-manager log 42                  # full bdfrx output of run 42
```

A lock file stops two runs from overlapping; a run that finds the lock exits with code 75 and does nothing. A run
exits with code 1 if any source failed.

When bdfrx reports that a user is suspended or does not exist, the manager marks that user `banned` and records why
in its note, so it is not retried every run.

Example crontab, every six hours:

```
0 */6 * * * /home/steve/bdfrx/bin/bdfrx-manager run >> /home/steve/.config/bdfrx/manager/cron.log 2>&1
```

## HTTP API

```bash
bdfrx-manager serve --host 0.0.0.0 --port 8765
```

Interactive docs are at `http://<host>:8765/docs` (and `/` redirects there).

Set an API key when serving beyond localhost: with `--api-key` or `$BDFRX_MANAGER_API_KEY` set, every request must
send it in an `X-API-Key` header (the docs page has an **Authorize** button for it), or in a `bdfrx_manager_key`
cookie, which is how the UI authorizes `<img>` and `<video>` requests that can't carry headers. Settings that name commands or
paths (`dest_root`, `staging_root`, `bdfrx_command`, `bdfrx_config`, `history_db_*`, `log_dir`) and per-source
`extra_args` are read-only over the API whether or not a key is set; change them on the server with
`bdfrx-manager config set` / `bdfrx-manager set`.

To keep it running as a systemd user service (with lingering enabled, it starts at boot):

```ini
# ~/.config/systemd/user/bdfrx-manager-api.service
[Unit]
Description=bdfrx manager API
After=network-online.target

[Service]
EnvironmentFile=%h/.config/bdfrx/api.env        # BDFRX_MANAGER_API_KEY=..., chmod 600
ExecStart=%h/bdfrx/bin/bdfrx-manager serve --host 0.0.0.0 --port 8765
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now bdfrx-manager-api
journalctl --user -u bdfrx-manager-api -f
```

| Method | Path                         | Purpose                                              |
|--------|------------------------------|------------------------------------------------------|
| GET    | `/status`                    | Source and rule counts, last run, run in progress    |
| GET    | `/sources`                   | List (`?kind=`, `?status=`, `?search=`)              |
| POST   | `/sources`                   | Add                                                  |
| GET    | `/sources/{kind}/{name}`     | One source, with effective options and destination   |
| PATCH  | `/sources/{kind}/{name}`     | Update; an explicit `null` clears an override        |
| DELETE | `/sources/{kind}/{name}`     | Remove                                               |
| GET    | `/rules`                     | List (`?kind=`)                                      |
| POST   | `/rules`                     | Add (`ignore_user` also pauses the source)           |
| DELETE | `/rules/{kind}/{value}`      | Remove                                               |
| GET    | `/settings`                  | All settings with defaults and help                  |
| PUT    | `/settings/{key}`            | Set; `{"value": null}` restores the default          |
| GET    | `/runs`                      | Recent runs (`?kind=`, `?name=`, `?status=`)         |
| GET    | `/runs/{id}`                 | One run                                              |
| GET    | `/runs/{id}/log`             | Full bdfrx output                                    |
| POST   | `/runs`                      | Start a run in the background (`dry_run` returns the plan) |
| GET    | `/candidates`                | Candidate users (`?min_posts=`, `?since=`, `?subreddit=`, `?limit=`) |
| GET    | `/candidates/{name}/posts`   | Their recent posts, each file with a `url` (`?limit=`) |
| POST   | `/candidates/{name}/promote` | Add as an active user source                         |
| GET    | `/candidates/dismissed`      | Dismissed users                                      |
| PUT    | `/candidates/{name}/dismissed` | Dismiss (optional `{"reason": ...}`)               |
| DELETE | `/candidates/{name}/dismissed` | Undismiss                                          |
| GET    | `/set-aside`                 | Users with files set aside                           |
| GET    | `/set-aside/{name}`          | How many of their files would move                   |
| POST   | `/set-aside/{name}`          | Move their files into the ignored folder             |
| DELETE | `/set-aside/{name}`          | Restore their set-aside files                        |
| GET    | `/posts/{id}/files/{n}`      | A recorded file of a post (supports `Range`, so video can seek) |
