# bdfrx-manager

`bdfrx-manager` keeps a list of users and subreddits to download in a SQLite database, and runs bdfrx for each of
them on a schedule. It replaces the old `data_local/{users,subreddits}/<name>` directory layout and the `update.sh`
wrapper scripts.

## Install

```bash
uv sync --locked --extra manager   # exact versions from uv.lock; the [manager] extra adds the web server
```

`pip install -e '.[manager]'` works too, without the pinned versions. For a full setup on a new machine (Reddit
login, data, settings, the service) or moving an existing one, see [INSTALL.md](INSTALL.md).

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
| `history_db_user`        | `~/.config/bdfrx/userdbs/{name}.db`          |
| `history_db_subreddit`   | empty: bdfrx's default database              |

Set `staging_root` when `dest_root` is a network share: files are downloaded locally, then moved into the archive
without overwriting anything already there. It is required when the share does not let you set file timestamps
(for example a CIFS mount where files are owned by root), because bdfrx stops with "Operation not permitted" when it
sets a downloaded file's date. When the manager runs on the machine that holds the archive, leave it
empty.

### History databases

bdfrx remembers what it has downloaded in a history DB. `{name}` in `history_db_user` or `history_db_subreddit`
becomes the source's name, giving each source its own DB. Per-user DBs are the default: each user's archive is then
complete by design (a shared DB would skip content someone else already posted), and user runs never contend for one
DB. Subreddits share one DB by default, since cross-posting makes shared de-duplication worthwhile there.

bdfrx commits after every post and waits up to 60 seconds for another process's write, so several runs can share one
DB; they only failed with "database is locked" when bdfrx held one transaction for a whole run.

To move from one shared user DB to per-user DBs:

```bash
bdfrx-manager split-user-history ~/.config/bdfrx/userdb/userdb --into '~/.config/bdfrx/userdbs/{name}.db' \
    --archive-root /mnt/c/bdfrx --dry-run
bdfrx-manager split-user-history ~/.config/bdfrx/userdb/userdb --into '~/.config/bdfrx/userdbs/{name}.db' \
    --archive-root /mnt/c/bdfrx
bdfrx-manager config set history_db_user '~/.config/bdfrx/userdbs/{name}.db'
```

The shared DB is only read. Rows are attributed to users by the `users/<name>/` in saved file paths and by the post
IDs of files in the archive's `users/<name>/` folders. Links, and posts marked done without a file (failed
downloads), can't be attributed and are left out; bdfrx checks post IDs first, so the only effect is that such failed
posts get one more attempt. Existing per-user DBs are added to, never replaced.

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
bdfrx-manager rule add exclude_title '\[M4' --reason "[M4F]-style tags"   # skip posts whose title matches
bdfrx-manager rule add exclude_flair '^meta$'        # skip posts whose flair matches
bdfrx-manager rule list
bdfrx-manager rule remove skip_domain example.com
bdfrx-manager set user someone --no-filters          # this source ignores the title and flair rules
```

Title and flair rules are regular expressions, searched for anywhere in the title or flair, ignoring case. Each run gets
them as bdfrx's `--exclude-title` and `--exclude-flair`, unless the source has them turned off (`--no-filters`, or the
switch in its Edit panel). For "only posts matching…", put bdfrx's `--include-title` or `--include-flair` in that
source's extra arguments. Upgrading from the old gender filter turns it into the equivalent title rule,
`\[M4.*?\]`, with the reason "was the gender filter"; a source that had it set to `none` gets filters off.

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
bdfrx-manager candidates promote someone --download   # ...and download their posts right away
bdfrx-manager candidates dismiss someone       # hide from the list; their posts still download
bdfrx-manager candidates undismiss someone
bdfrx-manager candidates dismissed
```

Each post's file locations are recorded too, so the API can serve them as previews; it only ever serves files it
has recorded. `scan-archive` fills in locations for posts recorded before they were kept.

Following someone from the web UI (or `POST /candidates/{name}/promote`) also starts downloading their posts in the
background, using the `follow_download_time` time window (default `all`, i.e. as far back as Reddit lists, about
1,000 posts) once; after that, scheduled runs use their normal settings.

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
**Undo** restores the files too. Changed your mind later? The **Rules** page lists everyone ignored, with a gallery of
their set-aside files to review, and **Unignore & restore files** undoes both at once (until you delete the files).

## Running

```bash
bdfrx-manager run --dry-run           # print the bdfrx commands it would run
bdfrx-manager run                     # every active source, least recently run first
bdfrx-manager run --kind user --sort top
bdfrx-manager run --name some_user
bdfrx-manager runs --failed           # recent failures
bdfrx-manager log 42                  # full bdfrx output of run 42
```

A lock file stops two scheduled runs from overlapping; a run that finds the lock exits with code 75 and does nothing.
Each source also has its own lock, so an on-demand download (from following someone, or `POST
/sources/{kind}/{name}/download`) runs alongside a scheduled run, and whichever reaches a source second skips it.
`bdfrx-manager run --time all` overrides the time window for one run. A run
exits with code 1 if any source failed.

When bdfrx reports that a user is suspended or does not exist, the manager marks that user `banned` and records why
in its note, so it is not retried every run.

Example crontab, every six hours:

```
0 */6 * * * /home/steve/bdfrx/bin/bdfrx-manager run >> /home/steve/.config/bdfrx/manager/cron.log 2>&1
```

## Concurrency, the queue and the schedule

At most `max_concurrent_downloads` bdfrx downloads (default 2) run at once, counted across every process: the API
server, `bdfrx-manager run` in a terminal, anything. Each download holds one of that many slot lock files in
`log_dir/slots/` and waits for one to be free. Every download is on one Reddit account, so keep this small.

The API server runs a queue that everything started from the web UI or API goes through: Download now, following a
candidate, adding a source with "download history now", Run all active, and the schedule. Waiting entries can be
cancelled (`DELETE /downloads/{id}`, or `DELETE /downloads` for all); one that has started finishes.

The queue is kept in the database, so it survives restarts, crashes and power cuts: waiting downloads stay queued,
and a download that was running is queued again and resumes (bdfrx's history skips what it already fetched). At
startup the server also closes runs left marked running and moves files left in the staging folder into the archive,
for any source no other process is downloading. `bdfrx-manager recover` does the same by hand.

Downloads you ask for (following someone, adding a source, Download now, Run again) go ahead of bulk ones (Run all,
the schedule); within each, first come, first served. Asking for a source that is already waiting in a bulk run moves
it to the front, with the wider of the two time windows.

When a site keeps refusing with HTTP 429 (three files in a row from the same host), bdfrx stops the run with exit code
75 instead of working through the rest against the same limit. The queue puts it at the back, not to start again for
`rate_limit_backoff_minutes` (default 30); nothing it couldn't fetch was recorded as done, so the next run gets it.
Asking for it by hand (Download now) lifts the wait.

Runs are incremental: each passes bdfrx `--since` set to the start of the source's last successful run, less
`incremental_overlap_hours` (default 48) to catch posts that appear late. With `--sort new` bdfrx then stops reading at
the first older post, so a routine run reads one page of the listing instead of everything Reddit lists (about 1,000
posts). A source's first run, and any all-time download (following someone, "Download all time"), read everything; an
explicit window ("last week") reads back that far. `incremental_runs false` turns this off.

Known limits: a file that failed temporarily is retried by later runs only while its post is inside their window, so
an old post (first reached by a full read) whose file failed may not be retried; and Reddit lists at most about 1,000
posts, which a full read can't go past and an incremental run could only hit for a source posting more than that
between runs. "Download all time" on a source re-reads everything Reddit lists and fills such gaps.

With `run_every_hours` set (e.g. `6`; `0` is off), the server queues every active source that often, least recently
run first. With `run_at` set (local time, e.g. `01:00`) it runs at that time and every `run_every_hours` from there,
restarting from `run_at` each day, in the machine's local time zone; a time missed by more than 3 hours (the server was
off) waits for the next one rather than catching up. The time it last did so is kept in the database, so restarting the server doesn't reset it.

## Web UI

`http://<host>:8765/` opens the web UI (it asks for the API key once per browser):

- **Candidates**: people posting in your subreddits, with previews; Follow (downloads their history), Dismiss, Ignore
- **Sources**: every source with a random thumbnail each visit, stats, Download now (normal, last week, all time),
  Pause/Resume, Edit (settings, stats, recent runs, and for users the subreddits they post in, to follow or skip),
  Ignore and Remove; Add and Run all active
- **Gallery** (click a source's thumbnail on Sources, or "Open gallery" in its Edit panel or Latest tab): everything in
  the source's archive folder, newest post first (by post ID), gallery posts grouped, filter images or videos, open in
  the full-screen viewer; `#gallery/u/name` or `#gallery/r/name`. Only files inside that source's own folder are served
- **Latest**: what runs downloaded, newest first. Posts from subreddits offer Follow/Ignore for their author; posts
  from people you follow offer Follow/Skip for their subreddit. Once followed: Resume (if paused) and Unfollow
- **The full-screen viewer**, wherever it's opened (Candidates, Latest, Gallery), has the same controls in its bar for
  the post on screen: who posted it or where, whether you follow, ignore or skip them, and the buttons to change that
- **Runs**: the activity panel (downloading, waiting, recently finished, with live logs and cancel); a summary of the
  last day or week with new files per day for 30 days; what needs attention (sources failing repeatedly, failed or
  interrupted runs, files that couldn't be fetched, deactivations); and the run history with filters, logs (errors
  and warnings highlighted) and Run again
- **Rules**: everything that stops downloads, each with its reason and an Undo. Ignored people show whether they're
  still a (paused) source and how many files are set aside, with a gallery to review them (`#gallery/ignored/name`);
  unignore them with or without restoring the files. Add or remove skipped subreddits, skipped websites and excluded
  posts (a Reddit link works), undismiss candidates, and search across every list
- **Settings**: every setting in plain language, saved as soon as it's changed, with its default and a reset link.
  Settings naming programs, folders or file naming are shown but locked (change them on the server with
  `bdfrx-manager config set`); `#settings/<name>` jumps to one

Run log files are deleted after `log_retention_days` (default 2); each run's result, counts and last lines stay in the
database. Runs record what started them (`trigger`), and how many files failed for good or will be retried.

Thumbnails are small JPEGs made with ffmpeg (a frame a second into videos) and cached in `log_dir/thumbs/`; without
ffmpeg, cards show the source's initial instead.

## HTTP API

```bash
bdfrx-manager serve --host 0.0.0.0 --port 8765
```

Interactive docs are at `http://<host>:8765/docs` (and `/` redirects there).

Set an API key when serving beyond localhost: with `--api-key` or `$BDFRX_MANAGER_API_KEY` set, every request must
send it in an `X-API-Key` header (the docs page has an **Authorize** button for it), or in a `bdfrx_manager_key`
cookie, which is how the UI authorizes `<img>` and `<video>` requests that can't carry headers. Settings that name commands or
paths (`dest_root`, `staging_root`, `bdfrx_command`, `bdfrx_config`, `history_db_*`, `log_dir`, `ignored_folder`), file
naming (`file_scheme`, `folder_scheme`), `authenticate`, and per-source `extra_args` are read-only over the API whether or not a key is set; change them on the server with
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
| GET    | `/runs`                      | Runs, newest first (`?kind=`, `?name=`, `?search=`, `?status=`, `?trigger=`, `?since=`, `?before_id=`) |
| GET    | `/runs/stats`                | Totals for the last day and week, per-day series, log disk use (`?days=`) |
| GET    | `/runs/attention`            | What needs attention                                 |
| POST   | `/runs/{id}/again`           | Queue that run's source again with the same time window |
| GET    | `/feed`                      | Latest downloaded posts (`?limit=`, `?before=` cursor, `?kind=`, `?name=`) |
| GET    | `/sources/user/{name}/subreddits` | Subreddits a user posts in, followed or skipped |
| GET    | `/runs/{id}`                 | One run                                              |
| GET    | `/runs/{id}/log`             | Full bdfrx output                                    |
| POST   | `/runs`                      | Queue every selected active source (`dry_run` returns the plan) |
| GET    | `/candidates`                | Candidate users (`?min_posts=`, `?since=`, `?subreddit=`, `?limit=`) |
| POST   | `/sources/{kind}/{name}/download` | Download one source now, in the background (`?time=`) |
| GET    | `/downloads`                 | The queue: running (with files so far), waiting, recently finished |
| DELETE | `/downloads/{id}`            | Cancel a waiting download (`DELETE /downloads` cancels all) |
| GET    | `/sources/{kind}/{name}/gallery` | Posts in the source's folder, newest first, with their author's and subreddit's status (`?offset=`, `?limit=`, `?order=`, `?media=`) |
| GET    | `/sources/{kind}/{name}/files/{file}` | A file from the source's folder (`/thumbnail` for a preview) |
| GET    | `/sources/{kind}/{name}/thumbnail` | A small preview of a random image or video of theirs |
| GET    | `/posts/{id}/files/{n}/thumbnail` | A small preview of one file                     |
| GET    | `/candidates/{name}/posts`   | Their recent posts, each file with a `url` (`?limit=`) |
| POST   | `/candidates/{name}/promote` | Add as an active user source and download them (`?download=false` to skip) |
| GET    | `/candidates/dismissed`      | Dismissed users                                      |
| PUT    | `/candidates/{name}/dismissed` | Dismiss (optional `{"reason": ...}`)               |
| DELETE | `/candidates/{name}/dismissed` | Undismiss                                          |
| GET    | `/set-aside`                 | Users with files set aside                           |
| GET    | `/set-aside/{name}`          | How many of their files would move                   |
| POST   | `/set-aside/{name}`          | Move their files into the ignored folder             |
| DELETE | `/set-aside/{name}`          | Restore their set-aside files                        |
| GET    | `/set-aside/{name}/gallery`  | Their set-aside files as posts, like a source's gallery |
| GET    | `/set-aside/{name}/files/{id}` | One set-aside file (`/thumbnail` for a preview)    |
| GET    | `/posts/{id}/files/{n}`      | A recorded file of a post (supports `Range`, so video can seek) |
