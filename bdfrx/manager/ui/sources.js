// The Sources page: every user and subreddit with stats, a random thumbnail, and actions (download now,
// pause/resume, edit, ignore, remove). The download queue itself is shown on the Runs page.

import { keepWatching, refresh as refreshDownloads, subscribe } from "./activity.js";
import {
  $,
  api,
  confirmIgnore,
  h,
  ignoreMessage,
  ignoreUser,
  number,
  queuedMessage,
  redditLink,
  sinceShort,
  sourceLabel,
  storage,
  toast,
  unignoreUser,
} from "./lib.js";
import { openLog } from "./logs.js";

const FILTER_STORAGE = "bdfrx_manager_source_filters";
const CHUNK = 40;
const SORTS = ["hot", "top", "new", "controversial", "rising", "relevance"];
const TIMES = ["all", "hour", "day", "week", "month", "year"];
const GENDERS = ["male", "female", "none"];

const page = {
  started: false,
  sources: [], // every source, from /sources
  shown: [], // filtered and sorted
  rendered: 0,
  cards: new Map(), // "kind/name" -> card element
  visit: "", // changes each visit so thumbnails are picked afresh
  settings: null,
  unsubscribe: null,
};

const keyOf = (source) => `${source.kind}/${source.name.toLowerCase()}`;
const galleryHash = (source) => `#gallery/${source.kind === "user" ? "u" : "r"}/${encodeURIComponent(source.name)}`;
const sourcePath = (source) => `/sources/${source.kind}/${encodeURIComponent(source.name)}`;

// Showing and hiding the page

export function showSources(visible) {
  if (!visible) {
    keepWatching(false);
    if (page.unsubscribe) page.unsubscribe();
    page.unsubscribe = null;
    return;
  }
  if (!page.started) setUp();
  page.visit = Math.random().toString(36).slice(2);
  keepWatching(true);
  page.unsubscribe = subscribe(markQueue);
  loadSources();
}

function setUp() {
  page.started = true;
  const form = $("#source-filters");
  try {
    const saved = JSON.parse(storage("get", FILTER_STORAGE) || "{}");
    for (const [name, value] of Object.entries(saved)) {
      if (name === "kind") {
        const radio = form.querySelector(`input[name=kind][value="${value}"]`);
        if (radio) radio.checked = true;
      } else if (form[name]) form[name].value = value;
    }
  } catch {
    // ignore unreadable saved filters
  }
  let debounce;
  form.addEventListener("change", applyFilters);
  form.addEventListener("input", (event) => {
    if (event.target.name !== "search") return;
    clearTimeout(debounce);
    debounce = setTimeout(applyFilters, 250);
  });
  form.addEventListener("submit", (event) => event.preventDefault());
  $("#add-source").addEventListener("click", openAddDialog);
  $("#run-all").addEventListener("click", runAll);
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) renderMore();
  }, { rootMargin: "800px" }).observe($("#source-sentinel"));
  // Close any other open menu when one opens
  document.addEventListener("toggle", (event) => {
    if (!event.target.matches?.("details.menu") || !event.target.open) return;
    for (const menu of document.querySelectorAll("details.menu[open]")) if (menu !== event.target) menu.open = false;
  }, true);
  document.addEventListener("click", (event) => {
    if (event.target.closest("details.menu")) return;
    for (const menu of document.querySelectorAll("details.menu[open]")) menu.open = false;
  });
  setUpAddDialog();
  setUpSheet();
}

// Loading, filtering and rendering sources

async function loadSources({ keepScroll = false } = {}) {
  try {
    [page.sources, page.settings] = await Promise.all([api("/sources"), page.settings ? page.settings : api("/settings")]);
  } catch (e) {
    toast(`Could not load sources: ${e.message}`, { error: true });
    return;
  }
  if (keepScroll) {
    // Update cards in place (stats after a download) without rebuilding the list
    const byKey = new Map(page.sources.map((s) => [keyOf(s), s]));
    for (const [key, card] of page.cards) if (byKey.has(key)) fillCard(card, byKey.get(key));
    updateCounts();
    return;
  }
  applyFilters();
}

function filters() {
  const form = $("#source-filters");
  return {
    search: form.search.value.trim().toLowerCase().replace(/^\/?(u|user|r)\//, ""),
    kind: form.querySelector("input[name=kind]:checked")?.value || "all",
    status: form.status.value,
    sort: form.sort.value,
  };
}

const SORTERS = {
  name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
  recent: (a, b) => (b.last_run_at || "").localeCompare(a.last_run_at || ""),
  stale: (a, b) => (a.last_run_at || "").localeCompare(b.last_run_at || ""),
  new_files: (a, b) => (b.last_files || 0) - (a.last_files || 0),
  total_files: (a, b) => b.total_files - a.total_files,
  failures: (a, b) => b.consecutive_failures - a.consecutive_failures,
};

function applyFilters() {
  const f = filters();
  storage("set", FILTER_STORAGE, JSON.stringify(f));
  updateCounts();
  page.shown = page.sources
    .filter((s) => (f.kind === "all" || s.kind === f.kind) && (f.status === "all" || s.status === f.status))
    .filter((s) => !f.search || s.name.toLowerCase().includes(f.search))
    .sort((a, b) => SORTERS[f.sort](a, b) || SORTERS.name(a, b));
  page.rendered = 0;
  page.cards.clear();
  $("#source-list").replaceChildren();
  renderMore();
}

function updateCounts() {
  const f = filters();
  const ofKind = page.sources.filter((s) => f.kind === "all" || s.kind === f.kind);
  const count = (status) => ofKind.filter((s) => status === "all" || s.status === status).length;
  for (const option of $("#source-filters").status.options) {
    option.textContent = `${option.dataset.label} (${number(count(option.value))})`;
  }
}

function renderMore() {
  const next = page.shown.slice(page.rendered, page.rendered + CHUNK);
  page.rendered += next.length;
  $("#source-list").append(...next.map(sourceCard));
  const total = page.shown.length;
  $("#source-status").textContent = total ? (page.rendered < total ? "" : `${number(total)} source${total === 1 ? "" : "s"}`) : "No sources match.";
  if (page.rendered < total) {
    const rect = $("#source-sentinel").getBoundingClientRect();
    if (rect.top < window.innerHeight + 800) requestAnimationFrame(renderMore);
  }
}

// replaceChildren would insert missing optional parts as the text "null"
function replaceWith(parent, ...children) {
  parent.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

// Cards

const thumbObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    thumbObserver.unobserve(entry.target);
    loadThumb(entry.target);
  }
}, { rootMargin: "200px" });

function loadThumb(box) {
  const img = h("img", { alt: "", decoding: "async", src: `${box.dataset.src}?v=${page.visit}` });
  img.addEventListener("load", () => box.classList.add("loaded"), { once: true });
  img.addEventListener("error", () => img.remove(), { once: true });
  box.append(img);
}

function sourceCard(source) {
  const card = h("article", { class: "source-card", "data-key": keyOf(source) });
  const thumb = h(
    "button",
    {
      type: "button",
      class: "thumb",
      "data-src": `${sourcePath(source)}/thumbnail`,
      title: "Open gallery",
      onclick: () => {
        location.hash = galleryHash(card.source);
      },
    },
    h("span", { class: "initial", "aria-hidden": "true" }, source.name.replace(/^[_-]+/, "").slice(0, 1).toUpperCase() || "?"),
  );
  card.append(thumb, h("div", { class: "source-body" }));
  thumbObserver.observe(thumb);
  fillCard(card, source);
  page.cards.set(keyOf(source), card);
  return card;
}

function lastRunText(source) {
  if (!source.last_run_at) return "never run";
  const mark = source.last_status === "ok" ? "✓" : "✗";
  const failures = source.consecutive_failures > 1 ? ` (${source.consecutive_failures} in a row)` : "";
  return `last run ${sinceShort(source.last_run_at)} ${mark}${failures}`;
}

function overrideChips(source) {
  const chips = [];
  if (source.sort) chips.push(`sort ${source.sort}`);
  if (source.time) chips.push(`time ${source.time}`);
  if (source.filter_gender) chips.push(`gender ${source.filter_gender}`);
  if (source.min_score !== null) chips.push(`min score ${source.min_score}`);
  if (source.post_limit !== null) chips.push(`limit ${source.post_limit}`);
  if (source.extra_args) chips.push("extra args");
  return chips.map((c) => h("span", { class: "chip" }, c));
}

function fillCard(card, source) {
  card.source = source;
  card.dataset.status = source.status;
  const body = card.querySelector(".source-body");
  const active = source.status === "active";
  replaceWith(body,
    h(
      "div",
      { class: "source-head" },
      h("a", { class: "source-name", href: redditLink(source.kind, source.name), target: "_blank", rel: "noopener noreferrer" }, sourceLabel(source.kind, source.name)),
      h("span", { class: `pill status-${source.status}` }, source.status),
      h("span", { class: "queue-state", hidden: true }),
    ),
    h(
      "div",
      { class: "source-meta" },
      lastRunText(source),
      source.last_run_at ? ` · ${number(source.last_files)} new` : "",
      ` · ${number(source.total_files)} total`,
    ),
    h("div", { class: "chips" }, overrideChips(source)),
    source.note ? h("div", { class: "note", title: source.note }, source.note) : null,
    h(
      "div",
      { class: "source-actions" },
      h(
        "div",
        { class: "split" },
        h("button", { type: "button", class: "primary", disabled: !active, onclick: () => download(source) }, "Download"),
        menu("More download options", "▾", [
          ["Download the last week", () => download(source, "week"), !active],
          ["Download all time", () => download(source, "all"), !active],
        ]),
      ),
      h("button", { type: "button", onclick: () => setStatus(source, active ? "paused" : "active") }, active ? "Pause" : "Resume"),
      h("button", { type: "button", onclick: () => openSheet(source) }, "Edit"),
      menu("More actions", "⋯", [
        source.kind === "user" ? ["Ignore everywhere…", () => ignoreSource(source)] : null,
        ["Remove…", () => removeSource(source), false, "danger"],
      ]),
    ),
  );
  markQueueState(card);
}

function menu(label, text, items) {
  return h(
    "details",
    { class: "menu" },
    h("summary", { "aria-label": label, title: label }, text),
    h(
      "div",
      { class: "menu-list" },
      items.filter(Boolean).map(([itemLabel, action, disabled, cls]) =>
        h("button", {
          type: "button",
          class: cls,
          disabled: Boolean(disabled),
          onclick: (event) => {
            event.target.closest("details").open = false;
            action();
          },
        }, itemLabel)),
    ),
  );
}

function markQueueState(card) {
  const label = card.querySelector(".queue-state");
  if (!label) return;
  const state = queueIndex.get(card.dataset.key);
  label.hidden = !state;
  label.textContent = state === "running" ? "downloading…" : state === "pending" ? "queued" : "";
  label.className = `queue-state ${state || ""}`;
}

// Actions

async function download(source, time) {
  const query = time ? `?time=${time}` : "";
  try {
    const { download: result } = await api(`${sourcePath(source)}/download${query}`, { method: "POST" });
    const what = time === "all" ? " (all time)" : time === "week" ? " (last week)" : "";
    toast(queuedMessage(result, sourceLabel(source.kind, source.name), what));
    refreshDownloads();
  } catch (e) {
    toast(`Could not queue ${sourceLabel(source.kind, source.name)}: ${e.message}`, { error: true });
  }
}

async function patchSource(source, fields) {
  const updated = await api(sourcePath(source), { method: "PATCH", body: fields });
  replaceSource(updated);
  return updated;
}

function replaceSource(updated) {
  const merged = { ...page.sources.find((s) => keyOf(s) === keyOf(updated)), ...updated };
  page.sources = page.sources.map((s) => (keyOf(s) === keyOf(updated) ? merged : s));
  const card = page.cards.get(keyOf(updated));
  if (card) fillCard(card, merged);
  updateCounts();
  return merged;
}

async function setStatus(source, status) {
  const before = source.status;
  try {
    await patchSource(source, { status });
  } catch (e) {
    toast(`Could not update ${sourceLabel(source.kind, source.name)}: ${e.message}`, { error: true });
    return;
  }
  toast(`${sourceLabel(source.kind, source.name)} ${status === "active" ? "resumed" : "paused"}`, {
    undo: () => patchSource(source, { status: before }),
  });
}

async function ignoreSource(source) {
  const options = await confirmIgnore(source.name);
  if (options === null) return;
  const before = source.status;
  let result;
  try {
    result = await ignoreUser(source.name, options);
  } catch (e) {
    toast(`Could not ignore u/${source.name}: ${e.message}`, { error: true });
    return;
  }
  await loadSources({ keepScroll: true });
  toast(ignoreMessage(source.name, result), {
    undo: async () => {
      await unignoreUser(source.name, options);
      await patchSource(source, { status: before });
    },
  });
}

async function removeSource(source) {
  const ok = await confirmDialog(
    `Remove ${sourceLabel(source.kind, source.name)}?`,
    "It stops being downloaded and leaves this list. Downloaded files and run history stay.",
    "Remove",
  );
  if (!ok) return;
  try {
    await api(sourcePath(source), { method: "DELETE" });
  } catch (e) {
    toast(`Could not remove it: ${e.message}`, { error: true });
    return;
  }
  page.sources = page.sources.filter((s) => keyOf(s) !== keyOf(source));
  page.cards.get(keyOf(source))?.remove();
  page.cards.delete(keyOf(source));
  updateCounts();
  const restore = {};
  for (const field of ["status", "sort", "time", "filter_gender", "min_score", "post_limit", "note"]) {
    if (source[field] !== null && source[field] !== undefined) restore[field] = source[field];
  }
  toast(`Removed ${sourceLabel(source.kind, source.name)}`, {
    undo: async () => {
      await api("/sources", { method: "POST", body: { kind: source.kind, name: source.name, ...restore } });
      await loadSources();
    },
  });
}

async function runAll() {
  const active = page.sources.filter((s) => s.status === "active").length;
  const slots = page.settings?.max_concurrent_downloads?.value || "2";
  const ok = await confirmDialog(
    `Queue all ${number(active)} active sources?`,
    `They download ${slots} at a time, least recently run first. Follow them, or cancel waiting ones, on the Runs page.`,
    "Queue all",
  );
  if (!ok) return;
  try {
    const counts = await api("/runs", { method: "POST", body: {} });
    const extra = [
      counts.already_queued ? `${number(counts.already_queued)} already queued` : "",
      counts.skipped ? `${number(counts.skipped)} skipped by rules` : "",
    ].filter(Boolean).join(", ");
    toast(`Queued ${number(counts.queued)}${extra ? ` · ${extra}` : ""}`);
    refreshDownloads();
  } catch (e) {
    toast(`Could not queue: ${e.message}`, { error: true });
  }
}

// A small reusable yes/no dialog

function confirmDialog(title, message, confirmLabel) {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  $("#confirm-ok").textContent = confirmLabel;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true }));
}

// Adding sources

function setUpAddDialog() {
  const form = $("#add-form");
  let touched = false;
  form.download.addEventListener("change", () => { touched = true; });
  form.addEventListener("change", (event) => {
    if (event.target.name === "kind" && !touched) form.download.checked = event.target.value === "user";
  });
  $("#add-dialog").addEventListener("close", async () => {
    const dialog = $("#add-dialog");
    if (dialog.returnValue !== "add") return;
    const kind = form.querySelector("input[name=kind]:checked").value;
    const names = [...new Set(form.names.value.split(/[\s,;]+/).map((n) => n.trim()).filter(Boolean))];
    const results = { added: 0, exists: 0, invalid: [], queued: 0 };
    for (const name of names) {
      try {
        const source = await api("/sources", { method: "POST", body: { kind, name, download: form.download.checked } });
        results.added += 1;
        if (source.download === "queued" || source.download === "moved up") results.queued += 1;
      } catch (e) {
        if (e.message.includes("already exists")) results.exists += 1;
        else results.invalid.push(name);
      }
    }
    const parts = [`Added ${results.added}`];
    if (results.queued) parts.push(`downloading ${results.queued}`);
    if (results.exists) parts.push(`${results.exists} already there`);
    if (results.invalid.length) parts.push(`not valid: ${results.invalid.join(", ")}`);
    toast(parts.join(" · "), { error: results.invalid.length > 0 && results.added === 0 });
    if (results.queued) refreshDownloads();
    if (results.added) loadSources();
  });
}

function openAddDialog() {
  const form = $("#add-form");
  form.reset();
  form.download.checked = form.querySelector("input[name=kind]:checked").value === "user";
  const dialog = $("#add-dialog");
  dialog.returnValue = "";
  dialog.showModal();
  form.names.focus();
}

// Detail and edit panel

function setUpSheet() {
  const form = $("#sheet-form");
  const choices = { sort: SORTS, time: TIMES, filter_gender: GENDERS };
  for (const [field, values] of Object.entries(choices)) {
    form[field].replaceChildren(h("option", { value: "" }, "Default"), ...values.map((v) => h("option", { value: v }, v)));
  }
  $("#sheet-close").addEventListener("click", () => $("#source-sheet").close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const source = $("#source-sheet").source;
    const number_ = (value) => (value.trim() === "" ? null : Number(value));
    const fields = {
      status: form.status.value,
      sort: form.sort.value || null,
      time: form.time.value || null,
      filter_gender: form.filter_gender.value || null,
      min_score: number_(form.min_score.value),
      post_limit: number_(form.post_limit.value),
      note: form.note.value.trim() || null,
    };
    try {
      const updated = replaceSource(await api(sourcePath(source), { method: "PATCH", body: fields }));
      toast(`Saved ${sourceLabel(source.kind, source.name)}`);
      openSheet(updated);
    } catch (e) {
      toast(`Could not save: ${e.message}`, { error: true });
    }
  });
}

async function openSheet(source) {
  const sheet = $("#source-sheet");
  sheet.source = source;
  const form = $("#sheet-form");
  $("#sheet-title").replaceChildren(
    h("a", { href: redditLink(source.kind, source.name), target: "_blank", rel: "noopener noreferrer" }, sourceLabel(source.kind, source.name)),
    " ",
    h("span", { class: `pill status-${source.status}` }, source.status),
    " ",
    h("a", { class: "see-all small", href: galleryHash(source), onclick: () => sheet.close() }, "Open gallery →"),
  );
  const thumb = $("#sheet-thumb");
  thumb.replaceChildren();
  const img = h("img", { alt: "", src: `${sourcePath(source)}/thumbnail?v=${Math.random().toString(36).slice(2)}` });
  img.addEventListener("error", () => img.remove(), { once: true });
  thumb.append(img);
  form.status.value = source.status;
  for (const field of ["sort", "time", "filter_gender"]) form[field].value = source[field] || "";
  form.min_score.value = source.min_score ?? "";
  form.post_limit.value = source.post_limit ?? "";
  form.note.value = source.note || "";
  $("#sheet-extra-args").textContent = source.extra_args || "none";
  $("#sheet-stats").replaceChildren(h("p", { class: "muted" }, "Loading…"));
  $("#sheet-runs").replaceChildren();
  $("#sheet-subreddits-block").hidden = source.kind !== "user";
  $("#sheet-subreddits").replaceChildren();
  if (!sheet.open) sheet.showModal();
  if (source.kind === "user") loadUserSubreddits(source);

  try {
    const [detail, runs] = await Promise.all([
      api(sourcePath(source)),
      api(`/runs?kind=${source.kind}&name=${encodeURIComponent(source.name)}&limit=8`),
    ]);
    if (sheet.source !== source) return;
    const effective = detail.effective_options;
    // Show what "Default" means for each setting
    for (const field of ["sort", "time", "filter_gender"]) form[field].options[0].textContent = `Default (${effective[field]})`;
    const row = (label, value) => [h("dt", {}, label), h("dd", {}, value)];
    $("#sheet-stats").replaceChildren(
      h(
        "dl",
        { class: "stats" },
        row("Runs", number(detail.total_runs)),
        row("Files downloaded", number(detail.total_files)),
        row("Last run", detail.last_run_at ? `${sinceShort(detail.last_run_at)} (${detail.last_status})` : "never"),
        row("Last success", sinceShort(detail.last_ok_at)),
        row("Failures in a row", number(detail.consecutive_failures)),
        row("Files go to", h("code", {}, detail.destination)),
      ),
    );
    $("#sheet-runs").replaceChildren(
      ...(runs.length
        ? runs.map((run) =>
            h(
              "li",
              {},
              h("span", { class: `run-status ${run.status}` }, run.status === "ok" ? "✓" : run.status === "running" ? "…" : "✗"),
              ` ${sinceShort(run.started_at)} · ${run.files_new === null ? "–" : number(run.files_new)} new `,
              h("button", { type: "button", class: "link", onclick: () => openLog(run) }, "log"),
            ))
        : [h("li", { class: "muted" }, "No runs yet")]),
    );
  } catch (e) {
    $("#sheet-stats").replaceChildren(h("p", { class: "muted" }, `Could not load details: ${e.message}`));
  }
}

// Marks on cards for sources that are downloading or queued

const queueIndex = new Map(); // "kind/name" -> "running" | "pending"
let seenFinished = null;

function markQueue(state) {
  queueIndex.clear();
  for (const job of state.pending) queueIndex.set(`${job.kind}/${job.name.toLowerCase()}`, "pending");
  for (const job of state.running) queueIndex.set(`${job.kind}/${job.name.toLowerCase()}`, "running");
  for (const card of page.cards.values()) markQueueState(card);
  // Refresh stats when downloads finish
  const finished = state.recent.map((j) => `${j.id}:${j.finished_at}`).join("|");
  if (seenFinished !== null && finished !== seenFinished) loadSources({ keepScroll: true });
  seenFinished = finished;
}

// Where a user posts, with follow or skip for each subreddit

async function loadUserSubreddits(source) {
  const list = $("#sheet-subreddits");
  list.replaceChildren(h("li", { class: "muted" }, "Loading…"));
  let subreddits;
  try {
    subreddits = await api(`${sourcePath(source)}/subreddits`);
  } catch (e) {
    list.replaceChildren(h("li", { class: "muted" }, `Could not load: ${e.message}`));
    return;
  }
  if ($("#source-sheet").source !== source) return;
  if (!subreddits.length) {
    list.replaceChildren(h("li", { class: "muted" }, "None recorded yet: this fills in as their posts are downloaded"));
    return;
  }
  list.replaceChildren(...subreddits.map((sub) => subredditRow(sub)));
}

function subredditRow(sub) {
  const row = h("li", {});
  const render = () => {
    const state = sub.status
      ? h("span", { class: `pill status-${sub.status}` }, sub.status === "active" ? "following" : sub.status)
      : sub.skipped
        ? h("span", { class: "pill status-paused" }, "skipped")
        : null;
    replaceWith(
      row,
      h("a", { class: "job-name", href: redditLink("subreddit", sub.name), target: "_blank", rel: "noopener noreferrer" }, `r/${sub.name}`),
      h("span", { class: "muted" }, `${number(sub.posts)} post${sub.posts === 1 ? "" : "s"}`),
      state,
      !sub.status && !sub.skipped ? h("button", { type: "button", class: "link", onclick: () => followSubreddit(sub, render) }, "follow") : null,
      !sub.status && !sub.skipped ? h("button", { type: "button", class: "link", onclick: () => skipSubreddit(sub, render) }, "skip") : null,
    );
  };
  render();
  return row;
}

// Following a subreddit downloads it straight away, like following a person
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
  if (!$("#view-sources").hidden) loadSources();
  toast(result.download ? `Following r/${sub.name} · downloading its posts` : `Following r/${sub.name}`, {
    undo: async () => {
      await api(`/sources/subreddit/${encodeURIComponent(sub.name)}`, { method: "DELETE" });
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
