# Web UI map

Names for every part of the manager's web UI, so we can talk about them the same way. **Bold** names are the ones to
use. They're the words on screen where there are any; where the screen has none, a name is given here. Each entry also
lists what it's called in the code (`#hash` address, element id, file), for finding it.

## Words that mean one thing

| Word | Means | Not to be confused with |
|---|---|---|
| **Source** | A user or subreddit the manager downloads from (Sources page) | a **candidate** |
| **Candidate** | Someone posting in your subreddits whom you don't follow yet | |
| **Follow** | Make someone or a subreddit an active source; it downloads straight away | |
| **Pause / Resume** | Stop or restart a source's downloads; it stays a source | **Remove** |
| **Remove** | Delete a source; its files stay | **Ignore** |
| **Ignore** | Never download anything a person posts, anywhere. Can **set aside** their files | **Dismiss** |
| **Set aside** | Move an ignored person's files into the archive's ignored folder; **restore** moves them back | deleting |
| **Dismiss** | Hide a candidate from Candidates; their posts still download from your subreddits | **Ignore** |
| **Skip** | Never download from a subreddit or website, whoever posted | **Remove** |
| **Exclude** | Never download one particular post | |
| **Rule** | Any ignore, skip or exclude (all listed on the Rules page) | |
| **Run** | One bdfrx download of one source, with its log and result (Runs → History) | **job** |
| **Job** | An entry in the **queue**: waiting, downloading, or recently finished; becomes a run once it starts | |
| **Queue** | All jobs: waiting and downloading (max 2 at once) | |
| **Schedule** | The automatic run of every active source (01:00, every 24 hours) | |
| **Gallery** | The Gallery page (a grid of everything in one source's folder) | **gallery post** |
| **Gallery post** | A Reddit post with several images; shown as one tile with a `1/N` badge | **Gallery** |
| **Viewer** | The full-screen view a tile opens; swipe or arrow keys step through files | |

## Everywhere

```
┌─ Top bar ───────────────────────────────────────────────────────────────────┐
│ bdfrx manager   Candidates Sources Latest Runs Rules Settings  [Activity pill] [Key] │
└─────────────────────────────────────────────────────────────────────────────┘
   page …
┌─ Bottom tabs (phones only) ─────────────────────────────────────────────────┐
└─────────────────────────────────────────────────────────────────────────────┘
                                                         [Toasts, bottom]
```

- **Top bar** (`.topbar`), with the **tabs** (`.tabs`), one per page
- **Activity pill** (`#activity`, activity.js): "Downloading u/a, r/b · 5 queued"; hidden when nothing is queued
- **Key button** (`#key-button`) → **Key dialog** (`#key-dialog`): enter or forget the API key
- **Bottom tabs** (`.bottom-tabs`): the same tabs at the bottom of the screen on phones
- **Toast** (`#toasts`): the message after an action, usually with **Undo**
- **Ignore dialog** (`#ignore-dialog`): "Ignore u/name?" with the **set-aside checkbox**
- **Confirm dialog** (`#confirm-dialog`): yes/no questions such as Remove
- **Viewer** (`#viewer`, viewer.js): **stage**, **‹ ›** arrows, and the **viewer bar** at the bottom. It holds the
  **info line** (subreddit link, score, date), the **viewer actions** (`#viewer-actions`: filled in by the Gallery,
  see there), the **counter** ("3 / 40") and Close. On phones the viewer actions get a row of their own. Toasts show
  above the bar while it's open
- **Log viewer** (`#log-dialog`, logs.js): a run's log, live while running, with **Next problem**

## Candidates `#candidates` (app.js)

- **Filter bar** (`#candidate-filters`): Min posts, Posted since, Subreddit
- **Candidate card** (`.card`): name, post count, score and last posted, **subreddit chips**, **preview strip**
  (`.strip`: their recent posts as thumbnails, open in the viewer), and **Follow / Dismiss / Ignore**

## Sources `#sources` (sources.js)

- **Filter bar** (`#source-filters`): Search, Kind (All / Users / Subreddits), Status, Sort, and the **toolbar
  buttons**: **+ Add** and **Run all active**
- **Source card** (`.source-card`):
  - **Card thumbnail**: a random image of theirs, a new one each visit; tap → **Gallery**
  - **Status pill** (active / paused / banned) and **queue mark** ("queued" / "downloading…")
  - **Card stats**: last run ✓/✗, new, total
  - **Override chips**: their settings that differ from the defaults (sort new, min score 10…)
  - **Note**
  - **Card buttons**: **Download** with its **▾ menu** (last week, all time), **Pause/Resume**, **Edit**, and the
    **⋯ menu** (Ignore everywhere…, Remove…)
- **Add dialog** (`#add-dialog`): Users/Subreddits, names, "Download their history now"
- **Edit panel** (`#source-sheet`, "sheet" in the code): opened by Edit. From top to bottom:
  - **panel thumbnail**
  - **stats**
  - **Settings form** (Status, Sort, Time window, Gender filter, Minimum score, Posts per run, Note)
  - for users, **Subreddits they post in**, each with follow / skip
  - **Recent runs**

## Latest `#latest` (latest.js)

What runs downloaded recently.

- **Filter bar** (`#latest-filters`): **Downloaded from** (Everything / Subreddits / People you follow), **Show** (By
  source / Newest first), Find a source
- **Source tabs** (`#latest-tabs`): **All**, then one tab per source with downloads in the last 7 days.
  - **Pinned** tabs (★) come first and stay even when quiet.
  - A **new dot** means new posts since you last opened that tab.
  - A **count** shows the number of posts.
- **All**, Show **By source**: a **source row** per source (`.latest-group`), with a name, new dot, "12 new · 3h",
  **See all →**, and a **row strip** of **row tiles** (caption: who posted, or where)
- **All**, Show **Newest first**: one feed of **post cards**
- A **source tab** (`#latest/u/name`, `#latest/r/name`): the **source header** (name, status, "N downloaded in the last
  7 days", **Gallery →**, **☆ Pin**), then that source's **post cards**
- **Post card** (`.post-card`): **post thumbnail** (opens the viewer), "u/author in r/sub", posted and downloaded times,
  and the **post buttons**:
  - from a subreddit: **Follow u/author** / **Ignore u/author**
  - from someone you follow: **Follow r/sub** / **Skip r/sub**
  - a **status pill** instead, when you already follow, skip or ignore them

## Gallery `#gallery/u/name`, `#gallery/r/name` (gallery.js)

Not a tab. Opened from a **card thumbnail**, **Gallery →** on Latest, or Edit.

- **Gallery header**: **← Sources**, name, "N posts · N files", **Latest downloads →**
- **Filter bar** (`#gallery-filters`): Show (Everything / Images / Videos), Order (Newest / Oldest first)
- **Gallery grid** of **gallery tiles**; ▶ marks videos, `1/N` marks gallery posts
- **Viewer actions**, for the post on screen:
  - in a subreddit's gallery: who posted it (**u/author**), a **status pill** (following / following, paused /
    ignored), and **Follow** (or **Resume**) / **Unfollow** / **Ignore**
  - in a person's gallery: the subreddit's **status pill** (following / skipped), or **Follow r/sub** / **Skip**
- **Set-aside gallery** `#gallery/ignored/name`: the same page for an ignored person's set-aside files. It has
  **← Rules**, and "· set aside" after the name

## Runs `#runs` (runs.js)

- **Activity panel** (`#activity-panel`, collapsible). From top to bottom:
  - **schedule line** ("Everything is queued every 24 h from 01:00 · next at … · change")
  - **Downloading** (each job with elapsed time, files so far, log)
  - **Waiting** (each with ✕, and **Cancel all**)
  - **Recently finished**
- **Summary** (`#runs-summary`):
  - **range switch** (last day / week)
  - **summary tiles**: new files, runs, failed runs, files not fetched, accounts deactivated
  - **chart** "New files per day, last 30 days"
- **Needs attention** (`#runs-attention`), in groups:
  - Failing repeatedly
  - Failed or interrupted
  - Files that couldn't be downloaded
  - Deactivated
- **History** (`#history-list`): **filter bar** (Source, Result, Kind, Started by, When), **run rows** (each with
  **log** and **Run again**), **Load older runs**

## Rules `#rules` (rules.js)

- **Ignore form** (`#rules-ignore-form`): Ignore someone, Why
- **Find box** (`#rules-search`): filters every list
- **Ignored people**: one row per person, showing:
  - **status pill** (whether they're still a source)
  - since, reason
  - "N files set aside · **review**" (→ set-aside gallery)
  - **Unignore & restore files** / **Unignore only**
- **Skipped subreddits**, **Skipped websites**, **Excluded posts**: each has an **add form** (value, Why, Add) and rows
  with **Remove**
- **Dismissed candidates**: rows with **Undismiss**

## Settings `#settings` (settings.js)

Grouped **setting rows** (label, help, control, "Reset to default"):

- **Downloading**
- **Defaults for sources**
- **Housekeeping**
- **Storage and commands**: **locked** settings, which can only be changed on the Pi
- **This device**: **Forget key**

`#settings/<name>` jumps to one setting and highlights it.
