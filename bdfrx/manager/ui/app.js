// bdfrx manager web UI. Plain ES modules, no dependencies; talks to the API on the same server.
// This file: tabs, the Candidates page and the full-screen viewer. Shared helpers are in lib.js, the download queue in
// activity.js, the Sources page in sources.js.

import { refresh as refreshDownloads } from "./activity.js";
import {
  $,
  api,
  askForKey,
  confirmIgnore,
  h,
  ignoreMessage,
  ignoreUser,
  number,
  setKey,
  storage,
  timeAgo,
  toast,
  unignoreUser,
} from "./lib.js";
import { showGallery } from "./gallery.js";
import { showLatest } from "./latest.js";
import { showRules } from "./rules.js";
import { showRuns } from "./runs.js";
import { showSettings } from "./settings.js";
import { showSources } from "./sources.js";
import { openViewer, setUpViewer, viewerItems } from "./viewer.js";

const FILTER_STORAGE = "bdfrx_manager_candidate_filters";
const PAGE_SIZE = 20;
const PREVIEW_POSTS = 8;

// Tabs

const VIEWS = {
  candidates: { title: "Candidates", show: showCandidates },
  sources: { title: "Sources", show: () => showSources(true), hide: () => showSources(false) },
  latest: { title: "Latest", show: (path) => showLatest(true, path), hide: () => showLatest(false) },
  runs: { title: "Runs", show: () => showRuns(true), hide: () => showRuns(false) },
  settings: { title: "Settings", show: (path) => showSettings(path) },
  gallery: { title: "Gallery", show: (path) => showGallery(path), tab: (path) => (path[0] === "ignored" ? "rules" : "sources") },
  rules: { title: "Rules", show: () => showRules() },
};
const TAB_TITLES = {};
let currentTab = null;

function route() {
  // "#latest/u/name" is the latest tab showing one source; the part after the tab name goes to the page
  const [first, ...path] = decodeURIComponent(location.hash.slice(1)).split("/");
  const tab = first || "candidates";
  // Pages without a tab of their own light up their parent's
  const parent = VIEWS[tab]?.tab;
  const navTab = (typeof parent === "function" ? parent(path) : parent) || tab;
  for (const link of document.querySelectorAll("[data-tab]")) {
    if (link.dataset.tab === navTab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const view of document.querySelectorAll(".view")) view.hidden = true;
  if (currentTab && currentTab !== tab && VIEWS[currentTab]?.hide) VIEWS[currentTab].hide();
  currentTab = tab;
  if (VIEWS[tab]) {
    $(`#view-${tab}`).hidden = false;
    document.title = `${VIEWS[tab].title} · bdfrx manager`;
    VIEWS[tab].show(path);
  } else {
    $("#view-placeholder").hidden = false;
    $("#placeholder-title").textContent = TAB_TITLES[tab] || "Not found";
    document.title = `${TAB_TITLES[tab] || "Not found"} · bdfrx manager`;
  }
}

// Candidates

const candidates = {
  loaded: false,
  offset: 0,
  done: false,
  loading: false,
  generation: 0,
};

function candidateFilters() {
  const form = $("#candidate-filters");
  return {
    min_posts: form.min_posts.value,
    since: form.since.value,
    subreddit: form.subreddit.value.trim().replace(/^\/?r\//i, ""),
  };
}

function showCandidates() {
  if (candidates.loaded) return;
  candidates.loaded = true;
  const form = $("#candidate-filters");
  try {
    const saved = JSON.parse(storage("get", FILTER_STORAGE) || "{}");
    for (const [name, value] of Object.entries(saved)) if (form[name]) form[name].value = value;
  } catch {
    // ignore unreadable saved filters
  }
  let debounce;
  form.addEventListener("change", () => resetCandidates());
  form.addEventListener("input", (event) => {
    if (event.target.name !== "subreddit") return;
    clearTimeout(debounce);
    debounce = setTimeout(resetCandidates, 400);
  });
  form.addEventListener("submit", (event) => event.preventDefault());
  api("/sources?kind=subreddit")
    .then((subs) => $("#subreddit-names").replaceChildren(...subs.map((s) => h("option", { value: s.name }))))
    .catch(() => {});
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadMoreCandidates();
  }, { rootMargin: "600px" }).observe($("#candidate-sentinel"));
  resetCandidates();
}

function resetCandidates() {
  storage("set", FILTER_STORAGE, JSON.stringify(candidateFilters()));
  candidates.generation += 1;
  candidates.offset = 0;
  candidates.done = false;
  candidates.loading = false;
  $("#candidate-list").replaceChildren();
  loadMoreCandidates();
}

async function loadMoreCandidates() {
  if (candidates.loading || candidates.done) return;
  candidates.loading = true;
  const generation = candidates.generation;
  const status = $("#candidate-status");
  status.textContent = "Loading…";
  const filters = candidateFilters();
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: candidates.offset, min_posts: filters.min_posts });
  if (filters.since) params.set("since", filters.since);
  if (filters.subreddit) params.set("subreddit", filters.subreddit);
  try {
    const page = await api(`/candidates?${params}`);
    if (generation !== candidates.generation) return; // filters changed while this was loading
    candidates.offset += page.length;
    candidates.done = page.length < PAGE_SIZE;
    $("#candidate-list").append(...page.map(candidateCard));
    const shown = $("#candidate-list").children.length;
    status.textContent = candidates.done ? (shown ? `${shown} candidate${shown === 1 ? "" : "s"}` : "No candidates match these filters.") : "";
  } catch (e) {
    if (generation !== candidates.generation) return;
    status.textContent = "";
    toast(`Could not load candidates: ${e.message}`, { error: true });
  } finally {
    if (generation === candidates.generation) candidates.loading = false;
  }
  // If the page did not fill the screen, the sentinel is still visible and will not fire again by itself
  if (!candidates.done && generation === candidates.generation) {
    const rect = $("#candidate-sentinel").getBoundingClientRect();
    if (rect.top < window.innerHeight + 600) loadMoreCandidates();
  }
}

const previewObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    previewObserver.unobserve(entry.target);
    loadPreviews(entry.target);
  }
}, { rootMargin: "300px" });

function candidateCard(candidate) {
  const subs = candidate.subreddits;
  const card = h(
    "article",
    { class: "card", "data-name": candidate.name },
    h(
      "div",
      { class: "card-head" },
      h("a", { class: "card-name", href: `https://www.reddit.com/user/${encodeURIComponent(candidate.name)}/submitted/`, target: "_blank", rel: "noopener noreferrer" }, `u/${candidate.name}`),
      h("span", { class: "card-count" }, `${number(candidate.posts)} post${candidate.posts === 1 ? "" : "s"}`),
    ),
    h(
      "div",
      { class: "card-meta" },
      `score ${number(candidate.total_score)} · last posted ${timeAgo(candidate.last_posted)}`,
      h("div", {}, ...subs.slice(0, 6).map((s) => h("span", { class: "chip" }, `r/${s}`)), subs.length > 6 ? h("span", { class: "chip" }, `+${subs.length - 6} more`) : null),
    ),
    h("div", { class: "strip", "data-name": candidate.name }),
    h(
      "div",
      { class: "card-actions" },
      h("button", { type: "button", class: "primary", onclick: () => act(card, "follow") }, "Follow"),
      h("button", { type: "button", onclick: () => act(card, "dismiss") }, "Dismiss"),
      h("button", { type: "button", class: "danger", onclick: () => act(card, "ignore") }, "Ignore"),
    ),
  );
  previewObserver.observe($(".strip", card));
  return card;
}

const ACTIONS = {
  follow: {
    run: async (name) => {
      const result = await api(`/candidates/${encodeURIComponent(name)}/promote?download=true`, { method: "POST" });
      if (result.download) refreshDownloads();
      return result;
    },
    // Removing the source also drops its download if it hasn't started (the worker re-checks before starting)
    undo: (name) => api(`/sources/user/${encodeURIComponent(name)}`, { method: "DELETE" }),
    message: (name, result) => (result && result.download ? `Following u/${name} · downloading their posts` : `Following u/${name}`),
  },
  dismiss: {
    run: (name) => api(`/candidates/${encodeURIComponent(name)}/dismissed`, { method: "PUT" }),
    undo: (name) => api(`/candidates/${encodeURIComponent(name)}/dismissed`, { method: "DELETE" }),
    message: (name) => `Dismissed u/${name}`,
  },
  ignore: {
    // Asks first, and can also move their files aside; returns what to say and how to undo
    confirm: confirmIgnore,
    run: ignoreUser,
    undo: unignoreUser,
    message: ignoreMessage,
  },
};

async function act(card, action) {
  const name = card.dataset.name;
  const { confirm, run, undo, message } = ACTIONS[action];
  const options = confirm ? await confirm(name) : {};
  if (options === null) return; // cancelled
  for (const button of card.querySelectorAll("button")) button.disabled = true;
  let result;
  try {
    result = await run(name, options);
  } catch (e) {
    for (const button of card.querySelectorAll("button")) button.disabled = false;
    toast(`Could not ${action} u/${name}: ${e.message}`, { error: true });
    return;
  }
  // Remember the card's place so undo can put it back
  const parent = card.parentNode;
  const next = card.nextSibling;
  card.classList.add("leaving");
  setTimeout(() => card.remove(), 200);
  candidates.offset = Math.max(0, candidates.offset - 1);
  toast(message(name, result), {
    undo: async () => {
      await undo(name, options);
      card.classList.remove("leaving");
      for (const button of card.querySelectorAll("button")) button.disabled = false;
      parent.insertBefore(card, next && next.parentNode === parent ? next : null);
      candidates.offset += 1;
    },
  });
}

// Previews

async function loadPreviews(strip) {
  const name = strip.dataset.name;
  let posts;
  try {
    posts = await api(`/candidates/${encodeURIComponent(name)}/posts?limit=${PREVIEW_POSTS}`);
  } catch (e) {
    strip.classList.add("none");
    toast(`Could not load previews for u/${name}: ${e.message}`, { error: true });
    return;
  }
  const items = viewerItems(posts);
  if (!items.length) {
    strip.classList.add("none");
    return;
  }
  // One tile per post (its first file); the viewer steps through every file
  const tiles = [];
  items.forEach((item, index) => {
    if (item.file.index !== 0) return;
    tiles.push(previewTile(item, () => openViewer(items, index)));
  });
  strip.replaceChildren(...tiles);
}


function previewTile({ post, file }, onclick) {
  const tile = h("button", { type: "button", class: "tile", title: `r/${post.subreddit} · ${timeAgo(post.posted_at)}`, onclick });
  if (file.kind === "image" || file.kind === "video") {
    // Small server-made previews; the viewer loads the full file
    const img = h("img", { src: `${file.url}/thumbnail`, loading: "lazy", alt: "", decoding: "async" });
    img.addEventListener("error", () => img.replaceWith(h("span", { class: "label" }, h("span", { class: "icon" }, "▢"), "No preview")), { once: true });
    tile.append(img);
    if (file.kind === "video") tile.append(h("span", { class: "play" }, "▶"));
  } else if (file.kind === "audio") {
    tile.append(h("span", { class: "label" }, h("span", { class: "icon" }, "♪"), "Audio"));
  } else if (file.kind === "text") {
    const snippet = h("span", { class: "snippet" }, "…");
    tile.append(h("span", { class: "label" }, snippet));
    fetch(file.url, { headers: { Range: "bytes=0-600" } })
      .then((r) => r.text())
      .then((text) => { snippet.textContent = text.trim() || "(empty)"; })
      .catch(() => { snippet.textContent = "Text post"; });
  } else {
    tile.append(h("span", { class: "label" }, h("span", { class: "icon" }, "▢"), file.name));
  }
  if (post.files.length > 1) tile.append(h("span", { class: "badge" }, `1/${post.files.length}`));
  return tile;
}

// Start

function setUpKeyDialog() {
  $("#key-button").addEventListener("click", async () => {
    await askForKey("Paste the API key from api.env on the server.");
    location.reload();
  });
  $("#key-forget").addEventListener("click", () => {
    setKey("");
    $("#key-dialog").close("cancel");
    toast("Key forgotten on this device");
  });
}

setUpKeyDialog();
setUpViewer();
refreshDownloads(); // picks up downloads already running, e.g. started from another device or by the schedule
window.addEventListener("hashchange", route);
route();
