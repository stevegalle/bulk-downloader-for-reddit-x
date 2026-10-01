# Setting up bdfrx-manager on a new machine

This covers a fresh install, and moving an existing setup (its sources, history and Reddit login) from another
machine. It assumes Linux with systemd; for a Synology NAS, a container is the better route.

Commands use `~/bdfrx-live` for the code and `~/bdfrx-data` for the manager's own files; any folders will do.

## 1. Prerequisites

- **Python 3.11 or newer**, **git**, and **ffmpeg** (thumbnails, and joining video and audio):
  ```bash
  sudo apt install git python3 ffmpeg
  ```
- **uv**, which installs the exact library versions in `uv.lock`:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  (or `pip install --user uv`)

## 2. Get the code and install it

```bash
git clone https://github.com/stevegalle/bulk-downloader-for-reddit-x.git ~/bdfrx-live
cd ~/bdfrx-live
git checkout manager
uv sync --locked --extra manager
```

This creates `~/bdfrx-live/.venv` with bdfrx, the manager and the web server. `--locked` installs exactly what
`uv.lock` lists, so the machine runs the same, tested versions as everywhere else. The commands are then:

```bash
~/bdfrx-live/.venv/bin/bdfrx-manager --help
~/bdfrx-live/.venv/bin/bdfrx --help
```

Add `--extra dev` to also get the test tools (`.venv/bin/pytest tests -m "not online and not reddit"`).

## 3. The Reddit login

bdfrx keeps its Reddit app credentials and login token in `~/.config/bdfrx/default_config.cfg`.

- **Moving from another machine:** *move* the file, don't copy it. bdfrx writes refreshed tokens back to the
  file it used, so two machines using copies can drift apart and one can end up with a token that no longer
  works. Stop the old machine first (section 7), then:
  ```bash
  mkdir -p ~/.config/bdfrx
  scp oldmachine:.config/bdfrx/default_config.cfg ~/.config/bdfrx/
  chmod 600 ~/.config/bdfrx/default_config.cfg
  ```
  and remove or rename it on the old machine. Keep a backup copy somewhere that nothing runs from.
- **New setup:** run `~/bdfrx-live/.venv/bin/bdfrx download /tmp/x --user me --authenticate --limit 1` once and
  follow bdfrx's instructions to authorise (see the README).

## 4. The manager's data

The manager keeps its list of sources, rules, run history and queue in one SQLite database, and bdfrx keeps
what it has downloaded in history databases (one per user, one for subreddits).

- **Fresh setup:** nothing to copy; the database is created on first use. Import an old `data_local` layout
  with `bdfrx-manager import-dirs`, and split an old shared user history with `split-user-history` (see
  [MANAGER.md](MANAGER.md)).
- **Moving from another machine:** with the old service stopped (so nothing is mid-write), copy the database
  and the history:
  ```bash
  mkdir -p ~/bdfrx-data
  rsync -a oldmachine:bdfrx-manager-test/manager.db ~/bdfrx-data/
  rsync -a oldmachine:bdfrx-manager-test/history/ ~/bdfrx-data/history/
  ```
  Copy any `manager.db-wal` file too if one exists. Run logs (`logs/`) aren't needed.

From here, point every command at the database:

```bash
export BDFRX_MANAGER_DB=~/bdfrx-data/manager.db
```

(the service in section 6 sets this for itself).

## 5. Settings for this machine

Where things live on this machine. These can only be changed here, not from the web page:

```bash
M=~/bdfrx-live/.venv/bin/bdfrx-manager
$M config set dest_root /mnt/c/bdfrx                      # the archive (users/ and subreddits/ inside)
$M config set staging_root ~/bdfrx-data/staging           # needed if the archive is a share that refuses
                                                          # to set file dates (e.g. CIFS owned by root);
                                                          # leave it empty to download straight into the archive
$M config set history_db_user '~/bdfrx-data/history/users/{name}.db'
$M config set history_db_subreddit ~/bdfrx-data/history/bdfrx.db
$M config set log_dir ~/bdfrx-data/logs
$M config set bdfrx_config ~/.config/bdfrx/default_config.cfg
$M config set bdfrx_command ''                            # empty: the bdfrx installed next to the manager
$M config list
```

**If the archive is at a different path than on the old machine**, the recorded file locations (used for
previews and set-aside) need rewriting once:

```bash
~/bdfrx-live/.venv/bin/python - <<'PY'
import os, sqlite3
OLD, NEW = "/old/archive", "/new/archive"
conn = sqlite3.connect(os.path.expanduser("~/bdfrx-data/manager.db"))
conn.execute("UPDATE post_files SET path = replace(path, ?, ?);", (OLD, NEW))
conn.execute("UPDATE set_aside SET from_path = replace(from_path, ?, ?), to_path = replace(to_path, ?, ?);",
             (OLD, NEW, OLD, NEW))
conn.commit()
PY
```

Check a source would run as expected:

```bash
$M run --dry-run --name u/someone
```

## 6. The web server as a service

An API key, so only you can use it from the network:

```bash
umask 077
echo "BDFRX_MANAGER_API_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" > ~/bdfrx-data/api.env
```

The service, as a systemd user service:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/bdfrx-manager-api.service <<'UNIT'
[Unit]
Description=bdfrx manager
After=network-online.target

[Service]
Environment=BDFRX_MANAGER_DB=%h/bdfrx-data/manager.db
EnvironmentFile=%h/bdfrx-data/api.env
ExecStart=%h/bdfrx-live/.venv/bin/bdfrx-manager serve --host 0.0.0.0 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
loginctl enable-linger "$USER"        # start at boot, without logging in
systemctl --user daemon-reload
systemctl --user enable --now bdfrx-manager-api
journalctl --user -u bdfrx-manager-api -f
```

Then open `http://<this machine>:8765/` and paste the key from `~/bdfrx-data/api.env`. If the archive is on a
network share, make sure it's mounted at boot before the service starts (e.g. an fstab entry with `_netdev`).

## 7. On the old machine (when moving)

```bash
systemctl --user disable --now bdfrx-manager-api
```

The queue is in the database, so anything waiting or interrupted carries on on the new machine. Once the new one
is running, remove the old Reddit config (section 3) so nothing there can use the token again.

## Updating

```bash
cd ~/bdfrx-live
git pull --ff-only
uv sync --locked --extra manager
systemctl --user restart bdfrx-manager-api
```

Downloads that were running resume by themselves after the restart. (With a separate development checkout, as on
the original machine, `update-live.sh` does this from the local `manager` branch instead of pulling.)

## Upgrading libraries

`uv.lock` pins every library. To move to newer versions, on a development checkout:

```bash
uv lock --upgrade-package yt-dlp      # one library (yt-dlp needs this most often, as sites change)
uv lock --upgrade                     # or everything
uv sync --locked --extra manager --extra dev
.venv/bin/pytest tests -m "not online and not reddit and not slow and not authenticated"
```

and commit `uv.lock` once the tests pass. `praw` is capped below 8, which removed something bdfrx relies on.
