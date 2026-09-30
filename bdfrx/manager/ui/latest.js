// The Latest page: what runs downloaded recently. A strip of tabs, one per source with downloads in the last 7 days
// (pinnable, with a dot for ones you haven't opened since they got new posts), and either every source as its own
// row ("By source") or one newest-first feed. A source's own tab (#latest/u/name, #latest/r/name) shows all its
// downloads as cards: posts found in subreddits offer to follow or ignore whoever posted them, and posts from people
// you follow offer to follow or skip the subreddit they were posted in.

import { refresh as refreshDownloads } from "./activity.js";
import {
  $,
  api,
  confirmIgnore,
  h,
  ignoreMessage,
  ignoreUser,
  number,
  redditLink,
  sinceShort,
  sourceLabel,
  storage,
  timeAgo,
  toast,
  unignoreUser,
} from "./lib.js";
import { followSubreddit, skipSubreddit } from "./sources.js";
import { openViewer, viewerItems } from "./viewer.js";

const DAYS = 7;
const PAGE_SIZE = 30;
const GROUP_PAGE = 10;
const PER_GROUP = 12;
const FILTER_STORAGE = "bdfrx_manager_latest_filter";
const LAYOUT_STORAGE = "bdfrx_manager_latest_layout";
const PIN_STORAGE = "bdfrx_manager_latest_pins";
const SEEN_STORAGE = "bdfrx_manager_latest_seen";

const page = {
  started: false,
  selected: null, // { kind, name } when a source's tab is open
  sources: [],
  generation: 0,
  // the card feed (a source's tab, or "Newest first")
  cursor: null,
  posts: [],
  // the grouped view
  groupOffset: 0,
  groupTotal: 0,
  done: false,
  loading: false,
};

const keyOf = (kind, name) => `${kind}/${name.toLowerCase()}`;
const hashFor = (source) => (source ? `#latest/${source.kind === "user" ? "u" : "r"}/${encodeURIComponent(source.name)}` : "#latest");

function readJson(key, fallback) {
  try {
    return JSON.parse(storage("get", key) || "null") ?? fallback;
  } catch {
    return fallback;
  }
}

const pins = () => readJson(PIN_STORAGE, []);
const seen = () => readJson(SEEN_STORAGE, {});

// Showing the page, from the address: #latest, #latest/u/name, #latest/r/name

export function showLatest(visible, path = []) {
  if (!visible) return;
  if (!page.started) setUp();
  const [prefix, name] = path;
  page.selected = name && (prefix === "u" || prefix === "r") ? { kind: prefix === "u" ? "user" : "subreddit", name } : null;
  load();
}

function setUp() {
  page.started = true;
  const form = $("#latest-filters");
  const savedFrom = storage("get", FILTER_STORAGE);
  if (savedFrom) form.querySelector(`input[name=from][value="${savedFrom}"]`)?.click();
  const savedLayout = storage("get", LAYOUT_STORAGE);
  if (savedLayout) form.querySelector(`input[name=layout][value="${savedLayout}"]`)?.click();
  form.addEventListener("change", (event) => {
    if (event.target.name === "from") storage("set", FILTER_STORAGE, event.target.value);
    if (event.target.name === "layout") storage("set", LAYOUT_STORAGE, event.target.value);
    if (event.target.name !== "find") load();
  });
  form.addEventListener("input", (event) => {
    if (event.target.name === "find") renderTabs();
  });
  form.addEventListener("submit", (event) => event.preventDefault());
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadMore();
  }, { rootMargin: "800px" }).observe($("#latest-sentinel"));
}

function kindFilter() {
  const from = $("#latest-filters").querySelector("input[name=from]:checked").value;
  return from === "all" ? null : from;
}

function layout() {
  return $("#latest-filters").querySelector("input[name=layout]:checked").value;
}

async function load() {
  page.generation += 1;
  page.cursor = null;
  page.posts = [];
  page.groupOffset = 0;
  page.done = false;
  page.loading = false;
  $("#latest-list").replaceChildren();
  $("#latest-groups").replaceChildren();
  $("#latest-layout").hidden = Boolean(page.selected);
  const generation = page.generation;
  try {
    const kind = kindFilter();
    page.sources = await api(`/feed/sources?days=${DAYS}${kind ? `&kind=${kind}` : ""}`);
  } catch (e) {
    toast(`Could not load sources: ${e.message}`, { error: true });
    page.sources = [];
  }
  if (generation !== page.generation) return;
  renderTabs();
  renderHeader();
  if (page.selected) markSeen(page.selected);
  loadMore();
}

// The tab strip

function renderTabs() {
  const find = $("#latest-filters").find.value.trim().toLowerCase().replace(/^\/?(u|user|r)\//, "");
  const pinned = pins();
  const seenCursors = seen();
  const byKey = new Map(page.sources.map((s) => [keyOf(s.kind, s.name), s]));
  const kind = kindFilter();
  // Pinned sources always show (even with nothing new), then the rest by most recent download
  const pinnedSources = pinned
    .map((key) => byKey.get(key) || { kind: key.split("/")[0], name: key.slice(key.indexOf("/") + 1), posts: 0, stale: true })
    .filter((s) => !kind || s.kind === kind);
  const rest = page.sources.filter((s) => !pinned.includes(keyOf(s.kind, s.name)));
  const matches = (s) => !find || s.name.toLowerCase().includes(find);
  const tab = (source) => {
    const key = source ? keyOf(source.kind, source.name) : null;
    const current = source ? page.selected && keyOf(page.selected.kind, page.selected.name) === key : !page.selected;
    const unseen = source && source.latest_cursor && source.latest_cursor > (seenCursors[key] || 0);
    return h(
      "a",
      { class: `source-tab${current ? " current" : ""}`, href: hashFor(source), "aria-current": current ? "page" : null },
      source && pinned.includes(key) ? h("span", { class: "pin-mark", "aria-label": "pinned" }, "★") : null,
      source ? sourceLabel(source.kind, source.name) : "All",
      source && !source.stale ? h("span", { class: "tab-count" }, number(source.posts)) : null,
      unseen ? h("span", { class: "unseen", title: "New since you last opened it" }) : null,
    );
  };
  $("#latest-tabs").replaceChildren(tab(null), ...pinnedSources.filter(matches).map(tab), ...rest.filter(matches).map(tab));
  $("#latest-tabs").querySelector(".current")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function markSeen(source) {
  const info = page.sources.find((s) => keyOf(s.kind, s.name) === keyOf(source.kind, source.name));
  if (!info) return;
  const all = seen();
  all[keyOf(source.kind, source.name)] = info.latest_cursor;
  storage("set", SEEN_STORAGE, JSON.stringify(all));
  renderTabs();
}

function togglePin(source) {
  const key = keyOf(source.kind, source.name);
  const all = pins();
  storage("set", PIN_STORAGE, JSON.stringify(all.includes(key) ? all.filter((k) => k !== key) : [key, ...all]));
  renderTabs();
  renderHeader();
}

function renderHeader() {
  const header = $("#latest-header");
  header.hidden = !page.selected;
  if (!page.selected) return;
  const source = page.selected;
  const info = page.sources.find((s) => keyOf(s.kind, s.name) === keyOf(source.kind, source.name));
  const pinned = pins().includes(keyOf(source.kind, source.name));
  replaceWith(
    header,
    h("a", { class: "latest-title", href: redditLink(source.kind, source.name), target: "_blank", rel: "noopener noreferrer" }, sourceLabel(source.kind, source.name)),
    info?.status ? h("span", { class: `pill status-${info.status}` }, info.status) : null,
    h("span", { class: "muted" }, info ? `${number(info.posts)} downloaded in the last ${DAYS} days · latest ${sinceShort(info.last_found)}` : `nothing downloaded in the last ${DAYS} days; older downloads below`),
    h("button", { type: "button", class: "pin-button", "aria-pressed": String(pinned), onclick: () => togglePin(source) }, pinned ? "★ Pinned" : "☆ Pin"),
  );
}

// replaceChildren would insert missing optional parts as the text "null"
function replaceWith(parent, ...children) {
  parent.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

// Loading: grouped rows, or a card feed

function loadMore() {
  if (!page.selected && layout() === "groups") return loadGroups();
  return loadCards();
}

async function loadCards() {
  if (page.loading || page.done) return;
  page.loading = true;
  const generation = page.generation;
  const params = new URLSearchParams({ limit: PAGE_SIZE });
  if (page.cursor !== null) params.set("before", page.cursor);
  if (page.selected) {
    params.set("kind", page.selected.kind);
    params.set("name", page.selected.name);
  } else if (kindFilter()) params.set("kind", kindFilter());
  $("#latest-status").textContent = "Loading…";
  try {
    const posts = await api(`/feed?${params}`);
    if (generation !== page.generation) return;
    page.done = posts.length < PAGE_SIZE;
    if (posts.length) page.cursor = posts[posts.length - 1].cursor;
    page.posts.push(...posts);
    $("#latest-list").append(...posts.map(postCard));
    const shown = page.posts.length;
    $("#latest-status").textContent = page.done
      ? shown
        ? `${number(shown)} post${shown === 1 ? "" : "s"}`
        : "Nothing downloaded yet: this fills in as runs download posts"
      : "";
  } catch (e) {
    if (generation === page.generation) $("#latest-status").textContent = `Could not load: ${e.message}`;
  } finally {
    if (generation === page.generation) page.loading = false;
  }
  keepFilling(generation);
}

async function loadGroups() {
  if (page.loading || page.done) return;
  page.loading = true;
  const generation = page.generation;
  const params = new URLSearchParams({ days: DAYS, per_source: PER_GROUP, limit: GROUP_PAGE, offset: page.groupOffset });
  if (kindFilter()) params.set("kind", kindFilter());
  $("#latest-status").textContent = "Loading…";
  try {
    const { total, groups } = await api(`/feed/groups?${params}`);
    if (generation !== page.generation) return;
    page.groupOffset += groups.length;
    page.groupTotal = total;
    page.done = page.groupOffset >= total;
    $("#latest-groups").append(...groups.map(groupSection));
    $("#latest-status").textContent = page.done
      ? total
        ? `${number(total)} source${total === 1 ? "" : "s"} with downloads in the last ${DAYS} days`
        : `Nothing downloaded in the last ${DAYS} days`
      : "";
  } catch (e) {
    if (generation === page.generation) $("#latest-status").textContent = `Could not load: ${e.message}`;
  } finally {
    if (generation === page.generation) page.loading = false;
  }
  keepFilling(generation);
}

// A page that didn't fill the screen leaves the sentinel visible, and it won't fire again by itself
function keepFilling(generation) {
  if (page.done || generation !== page.generation) return;
  if ($("#latest-sentinel").getBoundingClientRect().top < window.innerHeight + 800) loadMore();
}

// "By source": one row per source

function groupSection(group) {
  const source = { kind: group.kind, name: group.name };
  const unseen = group.latest_cursor > (seen()[keyOf(group.kind, group.name)] || 0);
  return h(
    "section",
    { class: "latest-group" },
    h(
      "div",
      { class: "group-head" },
      h("a", { class: "job-name", href: hashFor(source) }, sourceLabel(group.kind, group.name)),
      unseen ? h("span", { class: "unseen", title: "New since you last opened it" }) : null,
      h("span", { class: "muted" }, `${number(group.posts)} new · ${sinceShort(group.last_found)}`),
      h("a", { class: "see-all", href: hashFor(source) }, group.posts > group.items.length ? `See all ${number(group.posts)} →` : "Open →"),
    ),
    h(
      "div",
      { class: "group-strip" },
      group.items.map((post) => groupTile(group, post)),
    ),
  );
}

function groupTile(group, post) {
  const first = post.files[0];
  const caption = group.kind === "subreddit" ? `u/${post.author}` : `r/${post.subreddit}`;
  const tile = h("button", {
    type: "button",
    class: "group-tile",
    title: `${caption} · posted ${timeAgo(post.posted_at)}`,
    onclick: () => openViewer(viewerItems(group.items), viewerItems(group.items).findIndex((item) => item.post === post)),
  });
  tile.append(thumbnail(first, post.files.length), h("span", { class: "tile-caption" }, caption));
  return tile;
}

function thumbnail(file, count) {
  const box = h("span", { class: "thumb-box" });
  if (file && (file.kind === "image" || file.kind === "video")) {
    const img = h("img", { src: `${file.url}/thumbnail`, loading: "lazy", alt: "", decoding: "async" });
    img.addEventListener("load", () => box.classList.add("loaded"), { once: true });
    img.addEventListener("error", () => img.remove(), { once: true });
    box.append(img);
    if (file.kind === "video") box.append(h("span", { class: "play" }, "▶"));
  } else {
    box.append(h("span", { class: "initial" }, file?.kind === "audio" ? "♪" : file?.kind === "text" ? "¶" : "▢"));
  }
  if (count > 1) box.append(h("span", { class: "badge" }, `1/${count}`));
  return box;
}

// Cards (a source's tab, and "Newest first")

function postCard(post) {
  const card = h("article", { class: "post-card", "data-author": post.author.toLowerCase(), "data-subreddit": post.subreddit.toLowerCase() });
  card.post = post;
  const thumb = h("button", { type: "button", class: "post-thumb", title: "Open", onclick: () => openPost(post) });
  thumb.append(thumbnail(post.files[0], post.files.length));
  card.append(thumb, h("div", { class: "post-body" }));
  fillPost(card);
  return card;
}

function openPost(post) {
  // Step through this post's files, then on through the others loaded on the page
  const items = viewerItems(page.posts);
  openViewer(items, Math.max(0, items.findIndex((item) => item.post === post)));
}

function fillPost(card) {
  const post = card.post;
  const fromSubreddit = post.source_kind === "subreddit";
  const inSourceTab = Boolean(page.selected);
  replaceWith(
    card.querySelector(".post-body"),
    h(
      "div",
      { class: "post-head" },
      h("a", { class: "job-name", href: redditLink("user", post.author), target: "_blank", rel: "noopener noreferrer" }, `u/${post.author}`),
      h("span", { class: "muted" }, " in "),
      h("a", { href: redditLink("subreddit", post.subreddit), target: "_blank", rel: "noopener noreferrer" }, `r/${post.subreddit}`),
    ),
    h(
      "div",
      { class: "post-meta muted" },
      `posted ${timeAgo(post.posted_at)} · downloaded ${sinceShort(post.found_at)}`,
      fromSubreddit || inSourceTab ? "" : ` · following u/${post.source_name}`,
    ),
    h("div", { class: "post-actions" }, fromSubreddit ? authorActions(post) : subredditActions(post)),
  );
}

// Every card for the same author or subreddit changes together
function refreshCards(attribute, value) {
  for (const card of document.querySelectorAll(`#latest-list .post-card[data-${attribute}="${CSS.escape(value.toLowerCase())}"]`)) {
    fillPost(card);
  }
}

function setOnAll(match, fields) {
  for (const post of page.posts) if (match(post)) Object.assign(post, fields);
}

function authorActions(post) {
  if (post.author_status) return h("span", { class: `pill status-${post.author_status}` }, post.author_status === "active" ? "following" : post.author_status);
  if (post.author_ignored) return h("span", { class: "pill status-banned" }, "ignored");
  const sameAuthor = (p) => p.author.toLowerCase() === post.author.toLowerCase();
  return [
    h("button", {
      type: "button",
      class: "primary",
      onclick: async () => {
        let result;
        try {
          result = await api(`/candidates/${encodeURIComponent(post.author)}/promote?download=true`, { method: "POST" });
        } catch (e) {
          toast(`Could not follow u/${post.author}: ${e.message}`, { error: true });
          return;
        }
        setOnAll(sameAuthor, { author_status: "active" });
        refreshCards("author", post.author);
        if (result.download) refreshDownloads();
        toast(result.download ? `Following u/${post.author} · downloading their posts` : `Following u/${post.author}`, {
          undo: async () => {
            await api(`/sources/user/${encodeURIComponent(post.author)}`, { method: "DELETE" });
            setOnAll(sameAuthor, { author_status: null });
            refreshCards("author", post.author);
          },
        });
      },
    }, `Follow u/${post.author}`),
    h("button", {
      type: "button",
      class: "danger",
      onclick: async () => {
        const options = await confirmIgnore(post.author);
        if (options === null) return;
        let result;
        try {
          result = await ignoreUser(post.author, options);
        } catch (e) {
          toast(`Could not ignore u/${post.author}: ${e.message}`, { error: true });
          return;
        }
        setOnAll(sameAuthor, { author_ignored: true });
        refreshCards("author", post.author);
        toast(ignoreMessage(post.author, result), {
          undo: async () => {
            await unignoreUser(post.author, options);
            setOnAll(sameAuthor, { author_ignored: false });
            refreshCards("author", post.author);
          },
        });
      },
    }, "Ignore"),
  ];
}

function subredditActions(post) {
  if (post.subreddit.toLowerCase().startsWith("u_")) return h("span", { class: "muted small" }, "their profile");
  if (post.subreddit_status) {
    return h("span", { class: `pill status-${post.subreddit_status}` }, post.subreddit_status === "active" ? `following r/${post.subreddit}` : post.subreddit_status);
  }
  if (post.subreddit_skipped) return h("span", { class: "pill status-paused" }, `skipping r/${post.subreddit}`);
  // followSubreddit and skipSubreddit keep status/skipped on the object they're given; mirror it onto every post
  const sub = { name: post.subreddit, status: null, skipped: false };
  const sync = () => {
    setOnAll((p) => p.subreddit.toLowerCase() === post.subreddit.toLowerCase(), { subreddit_status: sub.status, subreddit_skipped: sub.skipped });
    refreshCards("subreddit", post.subreddit);
  };
  return [
    h("button", { type: "button", class: "primary", onclick: () => followSubreddit(sub, sync) }, `Follow r/${post.subreddit}`),
    h("button", { type: "button", onclick: () => skipSubreddit(sub, sync) }, "Skip"),
  ];
}
