// bdfrx manager web UI. Plain ES modules, no dependencies; talks to the API on the same server.

const KEY_STORAGE = "bdfrx_manager_key";
const KEY_COOKIE = "bdfrx_manager_key";
const FILTER_STORAGE = "bdfrx_manager_candidate_filters";
const SET_ASIDE_STORAGE = "bdfrx_manager_set_aside_on_ignore";
const PAGE_SIZE = 20;
const PREVIEW_POSTS = 8;

const $ = (selector, root = document) => root.querySelector(selector);

// Build an element: h("a", {href: "#", class: "x"}, "text", childElement). Text is never parsed as HTML.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
    else if (value === true) el.setAttribute(name, "");
    else el.setAttribute(name, value);
  }
  el.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return el;
}

function storage(action, key, value) {
  // localStorage can be unavailable (private windows, blocked site data); the UI still works, it just forgets
  try {
    if (action === "get") return localStorage.getItem(key);
    if (action === "set") localStorage.setItem(key, value);
    if (action === "remove") localStorage.removeItem(key);
  } catch {
    return null;
  }
  return null;
}

// API key: sent as a header on fetches, and mirrored into a cookie so <img>/<video> requests carry it too

function getKey() {
  return storage("get", KEY_STORAGE) || "";
}

function setKey(key) {
  if (key) {
    storage("set", KEY_STORAGE, key);
    document.cookie = `${KEY_COOKIE}=${encodeURIComponent(key)}; path=/; max-age=31536000; SameSite=Strict`;
  } else {
    storage("remove", KEY_STORAGE);
    document.cookie = `${KEY_COOKIE}=; path=/; max-age=0; SameSite=Strict`;
  }
}

// Re-assert the cookie on every load, in case it expired or was cleared while localStorage kept the key
if (getKey()) setKey(getKey());

let keyPrompt = null;

function askForKey(message) {
  if (keyPrompt) return keyPrompt;
  const dialog = $("#key-dialog");
  $("#key-message").textContent = message;
  const input = $("#key-form input[name=key]");
  input.value = "";
  dialog.returnValue = "";
  keyPrompt = new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => {
        keyPrompt = null;
        if (dialog.returnValue !== "cancel" && input.value.trim()) setKey(input.value.trim());
        resolve();
      },
      { once: true },
    );
  });
  dialog.showModal();
  input.focus();
  return keyPrompt;
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = "GET", body, retry = true } = {}) {
  const headers = {};
  const key = getKey();
  if (key) headers["X-API-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (response.status === 401 && retry) {
    await askForKey(key ? "That key was not accepted. Paste the key from api.env on the server." : "This server needs its API key. It is in api.env on the server.");
    return api(path, { method, body, retry: false });
  }
  if (!response.ok) {
    let message = await response.text();
    try {
      const detail = JSON.parse(message).detail;
      message = typeof detail === "string" ? detail : detail.map((d) => d.msg).join("; ");
    } catch {
      // plain-text error body
    }
    throw new ApiError(response.status, message || response.statusText);
  }
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

// Toasts, optionally with an undo button

function toast(message, { error = false, undo } = {}) {
  const el = h("div", { class: error ? "toast error" : "toast", role: error ? "alert" : "status" }, h("span", {}, message));
  let timer;
  const close = () => {
    clearTimeout(timer);
    el.remove();
  };
  if (undo) {
    el.append(
      h("button", { type: "button", onclick: async () => {
        close();
        try {
          await undo();
        } catch (e) {
          toast(`Undo failed: ${e.message}`, { error: true });
        }
      } }, "Undo"),
    );
  }
  $("#toasts").append(el);
  timer = setTimeout(close, undo ? 7000 : error ? 8000 : 3500);
}

// Formatting

function timeAgo(timestamp) {
  if (!timestamp) return "unknown";
  const then = new Date(`${timestamp.replace(" ", "T")}Z`);
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days < 1) return "today";
  if (days === 1) return "yesterday";
  if (days < 45) return `${days} days ago`;
  if (days < 540) return `${Math.round(days / 30)} months ago`;
  return `${(days / 365).toFixed(1)} years ago`;
}

const number = (n) => (n ?? 0).toLocaleString();

// Tabs

const VIEWS = { candidates: { title: "Candidates", show: showCandidates } };
const TAB_TITLES = { sources: "Sources", runs: "Runs", rules: "Rules", settings: "Settings" };

function route() {
  const tab = location.hash.slice(1) || "candidates";
  for (const link of document.querySelectorAll("[data-tab]")) {
    if (link.dataset.tab === tab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const view of document.querySelectorAll(".view")) view.hidden = true;
  if (VIEWS[tab]) {
    $(`#view-${tab}`).hidden = false;
    document.title = `${VIEWS[tab].title} · bdfrx manager`;
    VIEWS[tab].show();
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
      if (result.download) watchDownloads();
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
    run: async (name, { setAside }) => {
      await api("/rules", { method: "POST", body: { kind: "ignore_user", value: name } });
      if (!setAside) return { moved: 0, skipped: [] };
      return api(`/set-aside/${encodeURIComponent(name)}`, { method: "POST" });
    },
    undo: async (name, { setAside }) => {
      if (setAside) await api(`/set-aside/${encodeURIComponent(name)}`, { method: "DELETE" });
      await api(`/rules/ignore_user/${encodeURIComponent(name)}`, { method: "DELETE" });
    },
    message: (name, result) => {
      if (!result || !result.moved) return `Ignoring u/${name} everywhere`;
      const skipped = result.skipped.length ? `, ${result.skipped.length} skipped` : "";
      return `Ignoring u/${name} · moved ${number(result.moved)} file${result.moved === 1 ? "" : "s"} aside${skipped}`;
    },
  },
};

async function confirmIgnore(name) {
  const dialog = $("#ignore-dialog");
  const box = $("#ignore-set-aside");
  const label = $("#ignore-set-aside-label");
  $("#ignore-title").textContent = `Ignore u/${name}?`;
  box.checked = storage("get", SET_ASIDE_STORAGE) === "1";
  box.disabled = true;
  label.textContent = "Also move their downloaded files aside for review (counting…)";
  api(`/set-aside/${encodeURIComponent(name)}`)
    .then(({ files }) => {
      box.disabled = files === 0;
      if (files === 0) box.checked = false;
      label.textContent = files
        ? `Also move their ${number(files)} downloaded file${files === 1 ? "" : "s"} to the archive's ignored/${name} folder, to review and delete by hand`
        : "No downloaded files of theirs to move";
    })
    .catch((e) => { label.textContent = `Could not count their files: ${e.message}`; });
  dialog.returnValue = "";
  dialog.showModal();
  await new Promise((resolve) => dialog.addEventListener("close", resolve, { once: true }));
  if (dialog.returnValue !== "ignore") return null;
  const setAside = box.checked && !box.disabled;
  if (!box.disabled) storage("set", SET_ASIDE_STORAGE, box.checked ? "1" : "0");
  return { setAside };
}

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

// Background downloads (started by following someone): a status pill while any are queued or running

let downloadTimer = null;
let lastReported = null;

function describeJob(job) {
  return `${job.kind === "user" ? "u/" : "r/"}${job.name}`;
}

async function watchDownloads() {
  clearTimeout(downloadTimer);
  let state;
  try {
    state = await api("/downloads");
  } catch {
    downloadTimer = setTimeout(watchDownloads, 10000);
    return;
  }
  const pill = $("#activity");
  const busy = state.current || state.pending.length;
  if (busy) {
    const now = state.current ? `Downloading ${describeJob(state.current)}` : "Starting downloads";
    const waiting = state.pending.length ? ` · ${state.pending.length} queued` : "";
    pill.textContent = now + waiting;
    pill.title = [state.current, ...state.pending].filter(Boolean).map(describeJob).join(", ");
    pill.hidden = false;
  } else {
    pill.hidden = true;
  }
  // Report each finished download once
  const last = state.last;
  const key = last && `${last.kind}/${last.name}/${last.run_id ?? last.reason}`;
  if (last && lastReported !== null && key !== lastReported) {
    if (last.status === "ok") toast(`${describeJob(last)}: ${number(last.files_new)} new file${last.files_new === 1 ? "" : "s"}`);
    else if (last.status === "deactivated") toast(`${describeJob(last)} deactivated: ${last.reason}`, { error: true });
    else if (last.status === "skipped") toast(`${describeJob(last)}: download skipped (${last.reason})`);
    else toast(`${describeJob(last)}: download failed${last.reason ? ` (${last.reason})` : ""}. See the run's log.`, { error: true });
  }
  lastReported = key ?? lastReported ?? "";
  if (busy) downloadTimer = setTimeout(watchDownloads, 4000);
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

function viewerItems(posts) {
  return posts.flatMap((post) => post.files.map((file) => ({ post, file })));
}

function previewTile({ post, file }, onclick) {
  const tile = h("button", { type: "button", class: "tile", title: `r/${post.subreddit} · ${timeAgo(post.posted_at)}`, onclick });
  if (file.kind === "image") {
    tile.append(h("img", { src: file.url, loading: "lazy", alt: "", decoding: "async" }));
  } else if (file.kind === "video") {
    // #t= makes browsers show a frame from just past the start instead of a blank box
    tile.append(h("video", { src: `${file.url}#t=0.5`, preload: "metadata", muted: true, playsinline: true }), h("span", { class: "play" }, "▶"));
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

// Full-screen viewer

const viewer = { items: [], index: 0 };

function openViewer(items, index) {
  viewer.items = items;
  viewer.index = index;
  renderViewer();
  const dialog = $("#viewer");
  if (!dialog.open) dialog.showModal();
}

function renderViewer() {
  const { post, file } = viewer.items[viewer.index];
  const stage = $("#viewer-stage");
  let content;
  if (file.kind === "image") content = h("img", { src: file.url, alt: file.name });
  else if (file.kind === "video") content = h("video", { src: file.url, controls: true, autoplay: true, playsinline: true, loop: true });
  else if (file.kind === "audio") content = h("audio", { src: file.url, controls: true, autoplay: true });
  else if (file.kind === "text") {
    content = h("pre", {}, "Loading…");
    fetch(file.url, { headers: { Range: "bytes=0-100000" } })
      .then((r) => r.text())
      .then((text) => { content.textContent = text; })
      .catch((e) => { content.textContent = `Could not load: ${e.message}`; });
  } else content = h("a", { href: file.url, target: "_blank" }, `Open ${file.name}`);
  stage.replaceChildren(content);

  const gallery = post.files.length > 1 ? ` · image ${file.index + 1} of ${post.files.length}` : "";
  $("#viewer-info").replaceChildren(
    h("a", { href: `https://www.reddit.com/comments/${post.post_id}/`, target: "_blank", rel: "noopener noreferrer" }, `r/${post.subreddit}`),
    ` · score ${number(post.score)} · ${timeAgo(post.posted_at)}${gallery}`,
  );
  $("#viewer-count").textContent = `${viewer.index + 1} / ${viewer.items.length}`;
  $("#viewer-prev").disabled = viewer.index === 0;
  $("#viewer-next").disabled = viewer.index === viewer.items.length - 1;
}

function stepViewer(delta) {
  const index = viewer.index + delta;
  if (index < 0 || index >= viewer.items.length) return;
  viewer.index = index;
  renderViewer();
}

function closeViewer() {
  const dialog = $("#viewer");
  $("#viewer-stage").replaceChildren(); // stops any playing media
  if (dialog.open) dialog.close();
}

function setUpViewer() {
  const dialog = $("#viewer");
  $("#viewer-prev").addEventListener("click", () => stepViewer(-1));
  $("#viewer-next").addEventListener("click", () => stepViewer(1));
  $("#viewer-close").addEventListener("click", closeViewer);
  dialog.addEventListener("close", () => $("#viewer-stage").replaceChildren());
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") stepViewer(-1);
    if (event.key === "ArrowRight") stepViewer(1);
  });
  // Swipe left/right on touch screens
  let startX = null;
  let startY = null;
  const stage = $("#viewer-stage");
  stage.addEventListener("touchstart", (event) => {
    startX = event.touches[0].clientX;
    startY = event.touches[0].clientY;
  }, { passive: true });
  stage.addEventListener("touchend", (event) => {
    if (startX === null) return;
    const dx = event.changedTouches[0].clientX - startX;
    const dy = event.changedTouches[0].clientY - startY;
    startX = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) stepViewer(dx < 0 ? 1 : -1);
  });
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
watchDownloads(); // picks up downloads already running, e.g. started from another device
window.addEventListener("hashchange", route);
route();
