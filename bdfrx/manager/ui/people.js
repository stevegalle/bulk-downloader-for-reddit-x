// Follow, Resume, Unfollow, Ignore and Skip for the person who posted something or the subreddit it was posted in:
// the same controls on Latest's cards and in the full-screen viewer, wherever it was opened from.
//
// A post carries what's known about both: author_status, author_ignored, subreddit_status, subreddit_skipped. Its
// `via` (or the feed's source_kind) is the kind of source it was downloaded through. Posts found in a subreddit get
// controls for their author; posts from someone you follow get controls for their subreddit; via null gets none. After a change, onChange({ field, name, changes,
// moved }) is called so the page can update every post by that author ("author") or in that subreddit
// ("subreddit"). `moved` means files were set aside, so file listings changed.

import { refresh as refreshDownloads } from "./activity.js";
import { api, confirmIgnore, h, ignoreMessage, ignoreUser, redditLink, toast, unignoreUser } from "./lib.js";

const SOURCE_FIELDS = ["status", "sort", "time", "post_filters", "min_score", "post_limit", "note"];
const sourcePath = (kind, name) => `/sources/${kind}/${encodeURIComponent(name)}`;

// named: also show who or where, as a link (the viewer; cards already name both)
export function postControls(post, onChange, { named = false } = {}) {
  const via = post.via !== undefined ? post.via : post.source_kind;
  if (!via) return null;
  return via === "user" ? subredditControls(post, onChange, named) : authorControls(post, onChange, named);
}

function authorControls(post, onChange, named) {
  const name = post.author;
  if (!name) return null;
  const update = (changes, moved = false) => onChange({ field: "author", name, changes, moved });
  const link = named ? h("a", { href: redditLink("user", name), target: "_blank", rel: "noopener noreferrer" }, `u/${name}`) : null;
  if (post.author_ignored) return [link, h("a", { class: "pill status-banned", href: "#rules", title: "Change this on the Rules page" }, "ignored")];
  const status = post.author_status || null;
  return [
    link,
    status ? h("span", { class: `pill status-${status}` }, { active: "following", paused: "following, paused" }[status] || status) : null,
    !status ? h("button", { type: "button", class: "primary", onclick: () => followUser(name, update) }, named ? "Follow" : `Follow u/${name}`) : null,
    status === "paused" ? h("button", { type: "button", class: "primary", onclick: () => resume("user", name, update) }, "Resume") : null,
    status ? h("button", { type: "button", onclick: () => unfollow("user", name, update) }, "Unfollow") : null,
    h("button", { type: "button", class: "danger", onclick: () => ignore(name, status, update) }, "Ignore"),
  ];
}

function subredditControls(post, onChange, named) {
  const name = post.subreddit;
  if (!name) return null;
  if (name.toLowerCase().startsWith("u_")) return named ? null : h("span", { class: "muted small" }, "their profile");
  const update = (changes) => onChange({ field: "subreddit", name, changes, moved: false });
  const link = named ? h("a", { href: redditLink("subreddit", name), target: "_blank", rel: "noopener noreferrer" }, `r/${name}`) : null;
  if (post.subreddit_skipped) return [link, h("a", { class: "pill status-paused", href: "#rules", title: "Change this on the Rules page" }, "skipped")];
  const status = post.subreddit_status || null;
  const sub = { name, status, skipped: false };
  const done = () => update({ subreddit_status: sub.status, subreddit_skipped: sub.skipped });
  return [
    link,
    status ? h("span", { class: `pill status-${status}` }, { active: "following", paused: "following, paused" }[status] || status) : null,
    !status ? h("button", { type: "button", class: "primary", onclick: () => followSubreddit(sub, done) }, named ? "Follow" : `Follow r/${name}`) : null,
    status === "paused" ? h("button", { type: "button", class: "primary", onclick: () => resume("subreddit", name, update) }, "Resume") : null,
    status ? h("button", { type: "button", onclick: () => unfollow("subreddit", name, update) }, "Unfollow") : null,
    !status ? h("button", { type: "button", onclick: () => skipSubreddit(sub, done) }, "Skip") : null,
  ];
}

// Following someone downloads their history straight away (time window: follow_download_time)
async function followUser(name, update) {
  let result;
  try {
    result = await api(`/candidates/${encodeURIComponent(name)}/promote?download=true`, { method: "POST" });
  } catch (e) {
    toast(`Could not follow u/${name}: ${e.message}`, { error: true });
    return;
  }
  update({ author_status: "active" });
  if (result.download) refreshDownloads();
  toast(result.download ? `Following u/${name} · downloading their posts` : `Following u/${name}`, {
    // Removing the source also drops its download if it hasn't started
    undo: async () => {
      await api(sourcePath("user", name), { method: "DELETE" });
      update({ author_status: null });
    },
  });
}

async function resume(kind, name, update) {
  const field = kind === "user" ? "author_status" : "subreddit_status";
  const label = `${kind === "user" ? "u" : "r"}/${name}`;
  try {
    await api(sourcePath(kind, name), { method: "PATCH", body: { status: "active" } });
  } catch (e) {
    toast(`Could not resume ${label}: ${e.message}`, { error: true });
    return;
  }
  update({ [field]: "active" });
  toast(`Resumed ${label} · downloaded with everything else from the next scheduled run`, {
    undo: async () => {
      await api(sourcePath(kind, name), { method: "PATCH", body: { status: "paused" } });
      update({ [field]: "paused" });
    },
  });
}

// Unfollowing removes the source; its files stay, and Undo adds it back with the same settings
async function unfollow(kind, name, update) {
  const field = kind === "user" ? "author_status" : "subreddit_status";
  const label = `${kind === "user" ? "u" : "r"}/${name}`;
  let source;
  try {
    source = await api(sourcePath(kind, name));
    await api(sourcePath(kind, name), { method: "DELETE" });
  } catch (e) {
    toast(`Could not unfollow ${label}: ${e.message}`, { error: true });
    return;
  }
  update({ [field]: null });
  const restore = {};
  for (const key of SOURCE_FIELDS) if (source[key] !== null && source[key] !== undefined) restore[key] = source[key];
  toast(`Unfollowed ${label} · ${kind === "user" ? "their" : "its"} files stay`, {
    undo: async () => {
      await api("/sources", { method: "POST", body: { kind, name: source.name, ...restore } });
      update({ [field]: source.status });
    },
  });
}

// Ignoring someone you follow also pauses them; Undo resumes them
async function ignore(name, before, update) {
  const options = await confirmIgnore(name);
  if (options === null) return;
  let result;
  try {
    result = await ignoreUser(name, options);
  } catch (e) {
    toast(`Could not ignore u/${name}: ${e.message}`, { error: true });
    return;
  }
  const moved = Boolean(result.moved);
  update({ author_ignored: true, author_status: before === "active" ? "paused" : before }, moved);
  toast(ignoreMessage(name, result), {
    undo: async () => {
      await unignoreUser(name, options);
      if (before === "active") await api(sourcePath("user", name), { method: "PATCH", body: { status: "active" } });
      update({ author_ignored: false, author_status: before }, moved);
    },
  });
}

// For a subreddit { name, status, skipped }: these set status or skipped on it, then call done(). Following downloads
// it straight away, like following a person.
export async function followSubreddit(sub, done) {
  let result;
  try {
    result = await api("/sources", { method: "POST", body: { kind: "subreddit", name: sub.name, download: true } });
  } catch (e) {
    toast(`Could not follow r/${sub.name}: ${e.message}`, { error: true });
    return;
  }
  sub.status = "active";
  done();
  if (result.download) refreshDownloads();
  toast(result.download ? `Following r/${sub.name} · downloading its posts` : `Following r/${sub.name}`, {
    undo: async () => {
      await api(sourcePath("subreddit", sub.name), { method: "DELETE" });
      sub.status = null;
      done();
    },
  });
}

export async function skipSubreddit(sub, done) {
  try {
    await api("/rules", { method: "POST", body: { kind: "skip_subreddit", value: sub.name } });
  } catch (e) {
    toast(`Could not skip r/${sub.name}: ${e.message}`, { error: true });
    return;
  }
  sub.skipped = true;
  done();
  toast(`Skipping r/${sub.name}: posts there won't be downloaded, from anyone`, {
    undo: async () => {
      await api(`/rules/skip_subreddit/${encodeURIComponent(sub.name)}`, { method: "DELETE" });
      sub.skipped = false;
      done();
    },
  });
}

// Applies a change to every post it's about
export function applyChange(posts, { field, name, changes }) {
  const lower = name.toLowerCase();
  for (const post of posts) if ((post[field] || "").toLowerCase() === lower) Object.assign(post, changes);
}
