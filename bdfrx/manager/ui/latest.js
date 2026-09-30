// The Latest page: what recent runs downloaded, newest first, as thumbnails. Posts found in subreddits offer to
// follow or ignore the person who posted them; posts from people you follow offer to follow or skip the subreddit.

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
  storage,
  timeAgo,
  toast,
  unignoreUser,
} from "./lib.js";
import { followSubreddit, skipSubreddit } from "./sources.js";
import { openViewer, viewerItems } from "./viewer.js";

const FILTER_STORAGE = "bdfrx_manager_latest_filter";
const PAGE_SIZE = 30;

const page = { started: false, cursor: null, done: false, loading: false, generation: 0, posts: [] };

export function showLatest(visible) {
  if (!visible) return;
  if (!page.started) setUp();
  reset();
}

function setUp() {
  page.started = true;
  const form = $("#latest-filters");
  const saved = storage("get", FILTER_STORAGE);
  if (saved) form.querySelector(`input[value="${saved}"]`)?.click();
  form.addEventListener("change", () => {
    storage("set", FILTER_STORAGE, form.querySelector("input:checked").value);
    reset();
  });
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadMore();
  }, { rootMargin: "800px" }).observe($("#latest-sentinel"));
}

function reset() {
  page.generation += 1;
  page.cursor = null;
  page.done = false;
  page.loading = false;
  page.posts = [];
  $("#latest-list").replaceChildren();
  loadMore();
}

async function loadMore() {
  if (page.loading || page.done) return;
  page.loading = true;
  const generation = page.generation;
  const kind = $("#latest-filters").querySelector("input:checked").value;
  const params = new URLSearchParams({ limit: PAGE_SIZE });
  if (page.cursor !== null) params.set("before", page.cursor);
  if (kind !== "all") params.set("kind", kind);
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
  if (!page.done && generation === page.generation) {
    const rect = $("#latest-sentinel").getBoundingClientRect();
    if (rect.top < window.innerHeight + 800) loadMore();
  }
}

// replaceChildren would insert missing optional parts as the text "null"
function replaceWith(parent, ...children) {
  parent.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

function postCard(post) {
  const card = h("article", { class: "post-card", "data-author": post.author.toLowerCase(), "data-subreddit": post.subreddit.toLowerCase() });
  card.post = post;
  const first = post.files[0];
  const thumb = h("button", { type: "button", class: "post-thumb", title: "Open", onclick: () => openPost(post) });
  if (first && (first.kind === "image" || first.kind === "video")) {
    const img = h("img", { src: `${first.url}/thumbnail`, loading: "lazy", alt: "", decoding: "async" });
    img.addEventListener("load", () => thumb.classList.add("loaded"), { once: true });
    img.addEventListener("error", () => img.remove(), { once: true });
    thumb.append(img);
    if (first.kind === "video") thumb.append(h("span", { class: "play" }, "▶"));
  } else {
    thumb.append(h("span", { class: "initial" }, first?.kind === "audio" ? "♪" : first?.kind === "text" ? "¶" : "▢"));
  }
  if (post.files.length > 1) thumb.append(h("span", { class: "badge" }, `1/${post.files.length}`));
  card.append(thumb, h("div", { class: "post-body" }));
  fillPost(card);
  return card;
}

function openPost(post) {
  // Step through this post's files, then on through the others on the page
  const items = viewerItems(page.posts);
  const index = items.findIndex((item) => item.post === post);
  openViewer(items, Math.max(0, index));
}

function fillPost(card) {
  const post = card.post;
  const fromSubreddit = post.source_kind === "subreddit";
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
      fromSubreddit ? "" : ` · following u/${post.source_name}`,
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
