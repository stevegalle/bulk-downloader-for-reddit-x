// A source's gallery (#gallery/u/name, #gallery/r/name): everything in its archive folder as a grid of thumbnails,
// newest post first, with gallery posts grouped. #gallery/ignored/name shows someone's set-aside files the same way.
// Tapping one opens the full-screen viewer, whose bar shows who posted it (in a subreddit's gallery) or where (in a
// person's), with Follow, Unfollow, Ignore or Skip.

import { refresh as refreshDownloads } from "./activity.js";
import { $, api, confirmIgnore, h, ignoreMessage, ignoreUser, number, redditLink, sourceLabel, storage, toast, unignoreUser } from "./lib.js";
import { followSubreddit, skipSubreddit } from "./sources.js";
import { closeViewer, openViewer, refreshViewer, viewerItems } from "./viewer.js";

const PAGE_SIZE = 60;
const FILTER_STORAGE = "bdfrx_manager_gallery_filters";

const page = { started: false, source: null, offset: 0, total: 0, done: false, loading: false, generation: 0, posts: [] };

export function showGallery(path = []) {
  if (!page.started) setUp();
  const [prefix, name] = path;
  if (!name || !["u", "r", "ignored"].includes(prefix)) {
    location.hash = "#sources";
    return;
  }
  page.source = { kind: prefix === "r" ? "subreddit" : "user", name, setAside: prefix === "ignored" };
  document.title = `${sourceLabel(page.source.kind, name)} · ${page.source.setAside ? "set aside" : "gallery"} · bdfrx manager`;
  reset();
}

function setUp() {
  page.started = true;
  const form = $("#gallery-filters");
  try {
    const saved = JSON.parse(storage("get", FILTER_STORAGE) || "{}");
    for (const [name, value] of Object.entries(saved)) form.querySelector(`input[name="${name}"][value="${value}"]`)?.click();
  } catch {
    // ignore unreadable saved filters
  }
  form.addEventListener("change", () => {
    storage("set", FILTER_STORAGE, JSON.stringify(filters()));
    reset();
  });
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadMore();
  }, { rootMargin: "1000px" }).observe($("#gallery-sentinel"));
}

function filters() {
  const form = $("#gallery-filters");
  return {
    media: form.querySelector("input[name=media]:checked").value,
    order: form.querySelector("input[name=order]:checked").value,
  };
}

function reset() {
  page.generation += 1;
  page.offset = 0;
  page.total = 0;
  page.done = false;
  page.loading = false;
  page.posts = [];
  $("#gallery-grid").replaceChildren();
  const { kind, name, setAside } = page.source;
  $("#gallery-title").replaceChildren(
    h("a", { href: redditLink(kind, name), target: "_blank", rel: "noopener noreferrer" }, sourceLabel(kind, name)),
    setAside ? h("span", { class: "muted" }, " · set aside") : null,
  );
  $("#gallery-count").textContent = "";
  $("#gallery-latest").hidden = setAside;
  $("#gallery-latest").setAttribute("href", `#latest/${kind === "user" ? "u" : "r"}/${encodeURIComponent(name)}`);
  const back = $("#view-gallery .back-link");
  back.setAttribute("href", setAside ? "#rules" : "#sources");
  back.textContent = setAside ? "← Rules" : "← Sources";
  loadMore();
}

async function loadMore() {
  if (page.loading || page.done || !page.source) return;
  page.loading = true;
  const generation = page.generation;
  const { kind, name } = page.source;
  const params = new URLSearchParams({ offset: page.offset, limit: PAGE_SIZE, ...filters() });
  $("#gallery-status").textContent = "Loading…";
  try {
    const base = page.source.setAside ? `/set-aside/${encodeURIComponent(name)}` : `/sources/${kind}/${encodeURIComponent(name)}`;
    const result = await api(`${base}/gallery?${params}`);
    if (generation !== page.generation) return;
    page.offset += result.posts.length;
    page.total = result.total_posts;
    page.done = page.offset >= result.total_posts;
    page.posts.push(...result.posts);
    $("#gallery-grid").append(...result.posts.map(tile));
    $("#gallery-count").textContent = `${number(result.total_posts)} post${result.total_posts === 1 ? "" : "s"} · ${number(result.total_files)} file${result.total_files === 1 ? "" : "s"}`;
    $("#gallery-status").textContent =
      page.done && !page.total ? (page.source.setAside ? "No files set aside." : "Nothing in this source's folder yet.") : "";
  } catch (e) {
    if (generation === page.generation) $("#gallery-status").textContent = `Could not load: ${e.message}`;
  } finally {
    if (generation === page.generation) page.loading = false;
  }
  if (!page.done && generation === page.generation && $("#gallery-sentinel").getBoundingClientRect().top < window.innerHeight + 1000) {
    loadMore();
  }
}

function tile(post) {
  const first = post.files[0];
  const caption = page.source.kind === "user" ? (post.subreddit ? `r/${post.subreddit}` : first.name) : post.author ? `u/${post.author}` : first.name;
  const button = h("button", {
    type: "button",
    class: "gallery-tile",
    title: caption,
    onclick: () => {
      // The viewer steps through every file loaded so far, starting at this post
      const items = viewerItems(page.posts.map(asViewerPost));
      openViewer(items, Math.max(0, items.findIndex((item) => item.post.key === key(post))), { actions: viewerActions });
    },
  });
  const box = h("span", { class: "thumb-box" });
  if (first.kind === "image" || first.kind === "video") {
    const img = h("img", { src: `${first.url}/thumbnail`, loading: "lazy", alt: "", decoding: "async" });
    img.addEventListener("load", () => box.classList.add("loaded"), { once: true });
    img.addEventListener("error", () => img.remove(), { once: true });
    box.append(img);
    if (first.kind === "video") box.append(h("span", { class: "play" }, "▶"));
  } else {
    box.append(h("span", { class: "initial" }, first.kind === "audio" ? "♪" : first.kind === "text" ? "¶" : "▢"));
  }
  if (post.files.length > 1) box.append(h("span", { class: "badge" }, `1/${post.files.length}`));
  button.append(box);
  return button;
}

const key = (post) => post.post_id || post.files[0].name;

// The viewer expects feed-style posts; archive files have no recorded post date
const viewerPosts = new Map();
function asViewerPost(post) {
  if (!viewerPosts.has(post)) {
    viewerPosts.set(post, {
      key: key(post),
      post_id: post.post_id || "",
      subreddit: post.subreddit || page.source.name,
      score: post.score,
      posted_at: null,
      files: post.files,
      gallery: post,
    });
  }
  return viewerPosts.get(post);
}

// Actions in the viewer's bar

// Every loaded post by the same person, or in the same subreddit, changes together
function setOnAll(field, value, changes) {
  for (const post of page.posts) {
    if ((post[field] || "").toLowerCase() === value.toLowerCase()) Object.assign(post, changes);
  }
  refreshViewer();
}

function viewerActions(viewerPost) {
  const post = viewerPost.gallery;
  if (!post || page.source.setAside) return null;
  if (page.source.kind === "subreddit") return post.author ? authorActions(post) : null;
  return post.subreddit ? subredditActions(post) : null;
}

function authorActions(post) {
  const name = post.author;
  const link = h("a", { href: redditLink("user", name), target: "_blank", rel: "noopener noreferrer" }, `u/${name}`);
  if (post.author_ignored) return [link, h("a", { class: "pill status-banned", href: "#rules", title: "Change this on the Rules page" }, "ignored")];
  const status = post.author_status;
  const pill = status ? h("span", { class: `pill status-${status}` }, { active: "following", paused: "following, paused" }[status] || status) : null;
  return [
    link,
    pill,
    status !== "active" ? h("button", { type: "button", class: "primary", onclick: () => follow(name) }, status === "paused" ? "Resume" : "Follow") : null,
    status ? h("button", { type: "button", onclick: () => unfollow(name) }, "Unfollow") : null,
    h("button", { type: "button", class: "danger", onclick: () => ignore(name) }, "Ignore"),
  ];
}

async function follow(name) {
  const before = page.posts.find((p) => p.author?.toLowerCase() === name.toLowerCase())?.author_status || null;
  let result;
  try {
    result = await api(`/candidates/${encodeURIComponent(name)}/promote?download=true`, { method: "POST" });
  } catch (e) {
    toast(`Could not follow u/${name}: ${e.message}`, { error: true });
    return;
  }
  setOnAll("author", name, { author_status: "active" });
  if (result.download) refreshDownloads();
  toast(result.download ? `Following u/${name} · downloading their posts` : `Following u/${name}`, {
    undo: async () => {
      if (before) await api(`/sources/user/${encodeURIComponent(name)}`, { method: "PATCH", body: { status: before } });
      else await api(`/sources/user/${encodeURIComponent(name)}`, { method: "DELETE" });
      setOnAll("author", name, { author_status: before });
    },
  });
}

async function unfollow(name) {
  let source;
  try {
    source = await api(`/sources/user/${encodeURIComponent(name)}`);
    await api(`/sources/user/${encodeURIComponent(name)}`, { method: "DELETE" });
  } catch (e) {
    toast(`Could not unfollow u/${name}: ${e.message}`, { error: true });
    return;
  }
  setOnAll("author", name, { author_status: null });
  // Undo adds them back with the same settings
  const restore = {};
  for (const field of ["status", "sort", "time", "filter_gender", "min_score", "post_limit", "note"]) {
    if (source[field] !== null && source[field] !== undefined) restore[field] = source[field];
  }
  toast(`Unfollowed u/${name} · their files stay`, {
    undo: async () => {
      await api("/sources", { method: "POST", body: { kind: "user", name: source.name, ...restore } });
      setOnAll("author", name, { author_status: source.status });
    },
  });
}

async function ignore(name) {
  const options = await confirmIgnore(name);
  if (options === null) return;
  const before = page.posts.find((p) => p.author?.toLowerCase() === name.toLowerCase())?.author_status || null;
  let result;
  try {
    result = await ignoreUser(name, options);
  } catch (e) {
    toast(`Could not ignore u/${name}: ${e.message}`, { error: true });
    return;
  }
  // Ignoring someone you follow pauses them
  setOnAll("author", name, { author_ignored: true, author_status: before === "active" ? "paused" : before });
  if (result.moved) {
    // Their files left this folder, so the gallery no longer matches it
    closeViewer();
    reset();
  }
  toast(ignoreMessage(name, result), {
    undo: async () => {
      await unignoreUser(name, options);
      if (before === "active") await api(`/sources/user/${encodeURIComponent(name)}`, { method: "PATCH", body: { status: "active" } });
      setOnAll("author", name, { author_ignored: false, author_status: before });
      if (result.moved) reset();
    },
  });
}

function subredditActions(post) {
  const name = post.subreddit;
  if (name.toLowerCase().startsWith("u_")) return null; // their own profile
  if (post.subreddit_status) {
    return h("span", { class: `pill status-${post.subreddit_status}` }, post.subreddit_status === "active" ? "following" : post.subreddit_status);
  }
  if (post.subreddit_skipped) return h("a", { class: "pill status-paused", href: "#rules", title: "Change this on the Rules page" }, "skipped");
  // followSubreddit and skipSubreddit keep status/skipped on the object they're given
  const sub = { name, status: null, skipped: false };
  const sync = () => setOnAll("subreddit", name, { subreddit_status: sub.status, subreddit_skipped: sub.skipped });
  return [
    h("button", { type: "button", class: "primary", onclick: () => followSubreddit(sub, sync) }, `Follow r/${name}`),
    h("button", { type: "button", onclick: () => skipSubreddit(sub, sync) }, "Skip"),
  ];
}
