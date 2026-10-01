// The Runs page: the live download queue (activity panel), how things are going (summary and a chart of new files
// per day), what needs attention, and the history of runs with their logs.

import { cancel, keepWatching, refresh as refreshDownloads, subscribe } from "./activity.js";
import { $, api, duration, h, number, queuedMessage, redditLink, sinceShort, sourceLabel, storage, toast } from "./lib.js";
import { openLog, setUpLogDialog } from "./logs.js";

const PANEL_STORAGE = "bdfrx_manager_activity_open";
const RANGE_STORAGE = "bdfrx_manager_summary_range";
const HISTORY_STORAGE = "bdfrx_manager_run_filters";
const WAITING_SHOWN = 5;
const HISTORY_PAGE = 50;
const TRIGGERS = {
  follow: "followed",
  added: "added",
  manual: "download now",
  schedule: "schedule",
  "run-all": "run all",
  cli: "command line",
};

const page = { started: false, unsubscribe: null, ticker: null, stats: null, oldestId: null, loadingHistory: false };

export function showRuns(visible) {
  if (!visible) {
    keepWatching(false);
    if (page.unsubscribe) page.unsubscribe();
    page.unsubscribe = null;
    clearInterval(page.ticker);
    return;
  }
  if (!page.started) setUp();
  keepWatching(true);
  page.unsubscribe = subscribe(renderActivity);
  page.ticker = setInterval(tickElapsed, 1000);
  loadSchedule();
  loadSummary();
  loadAttention();
  loadHistory({ reset: true });
}

function setUp() {
  page.started = true;
  setUpLogDialog();
  const panel = $("#activity-panel");
  if (storage("get", PANEL_STORAGE) === "0") panel.open = false;
  panel.addEventListener("toggle", () => storage("set", PANEL_STORAGE, panel.open ? "1" : "0"));
  $("#activity-cancel-all").addEventListener("click", async () => {
    await cancel();
    toast("Cancelled every waiting download");
  });
  const range = $("#summary-range");
  const savedRange = storage("get", RANGE_STORAGE);
  if (savedRange) range.querySelector(`input[value="${savedRange}"]`)?.click();
  range.addEventListener("change", () => {
    storage("set", RANGE_STORAGE, range.querySelector("input:checked").value);
    renderTotals();
  });
  const form = $("#run-filters");
  try {
    const saved = JSON.parse(storage("get", HISTORY_STORAGE) || "{}");
    for (const [name, value] of Object.entries(saved)) if (form[name]) form[name].value = value;
  } catch {
    // ignore unreadable saved filters
  }
  let debounce;
  form.addEventListener("change", () => loadHistory({ reset: true }));
  form.addEventListener("input", (event) => {
    if (event.target.name !== "name") return;
    clearTimeout(debounce);
    debounce = setTimeout(() => loadHistory({ reset: true }), 350);
  });
  form.addEventListener("submit", (event) => event.preventDefault());
  $("#history-more").addEventListener("click", () => loadHistory({ reset: false }));
  window.addEventListener("resize", () => {
    if (!$("#view-runs").hidden && page.stats) renderChart(page.stats.per_day);
  });
}

// replaceChildren would insert missing optional parts as the text "null"
function replaceWith(parent, ...children) {
  parent.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

const runLink = (run) => h("button", { type: "button", class: "link", onclick: () => openLog(run) }, "log");

async function runAgain(run) {
  try {
    const { download: result, time } = await api(`/runs/${run.id}/again`, { method: "POST" });
    const label = sourceLabel(run.source_kind, run.source_name);
    toast(queuedMessage(result, label, time ? ` (${time})` : ""));
    refreshDownloads();
  } catch (e) {
    toast(`Could not run it again: ${e.message}`, { error: true });
  }
}

// Activity panel: what is running, waiting and recently finished

let showAllWaiting = false;
let lastState = null;
let seenFinished = null;

function renderActivity(state) {
  lastState = state;
  if ($("#view-runs").hidden) return;
  const busy = state.running.length + state.pending.length;
  $("#activity-summary").textContent = busy
    ? `${state.running.length} downloading · ${state.pending.length} waiting · up to ${state.slots} at once`
    : state.recent.length
      ? `Idle · last finished ${sinceShort(state.recent[0].finished_at)}`
      : "Idle";
  $("#activity-cancel-all").hidden = state.pending.length === 0;
  $("#activity-panel").classList.toggle("busy", busy > 0);

  const label = (job) => h("span", { class: "job-name" }, sourceLabel(job.kind, job.name));
  const trigger = (job) =>
    h("span", { class: "chip" }, `${TRIGGERS[job.trigger] || job.trigger}${job.resumed ? " · resumed after a restart" : ""}`);
  const logButton = (job) => (job.run_id ? runLink({ id: job.run_id, source_kind: job.kind, source_name: job.name }) : null);

  replaceWith(
    $("#activity-running"),
    state.running.map((job) =>
      h(
        "li",
        {},
        h("span", { class: "spinner", "aria-hidden": "true" }),
        label(job),
        trigger(job),
        h("span", { class: "muted elapsed", "data-since": job.started_at }, duration(job.started_at)),
        job.run_id === null ? h("span", { class: "muted" }, "waiting for a slot") : null,
        job.files_so_far !== null && job.files_so_far !== undefined ? h("span", { class: "muted" }, `${number(job.files_so_far)} files so far`) : null,
        logButton(job),
      )),
  );
  const waiting = showAllWaiting ? state.pending : state.pending.slice(0, WAITING_SHOWN);
  replaceWith(
    $("#activity-pending"),
    waiting.map((job) =>
      h(
        "li",
        {},
        label(job),
        trigger(job),
        job.not_before && new Date(`${job.not_before.replace(" ", "T")}Z`) > Date.now()
          ? h("span", { class: "muted warn-text" }, `rate limited · waits until ${localTime(job.not_before)}`)
          : h("span", { class: "muted" }, `queued ${sinceShort(job.queued_at)}`),
        h("button", { type: "button", class: "icon-button", title: "Cancel", "aria-label": `Cancel ${sourceLabel(job.kind, job.name)}`, onclick: () => cancel(job.id) }, "✕"),
      )),
    state.pending.length > WAITING_SHOWN
      ? h("li", {}, h("button", {
          type: "button",
          class: "link",
          onclick: () => {
            showAllWaiting = !showAllWaiting;
            renderActivity(lastState);
          },
        }, showAllWaiting ? "Show fewer" : `Show all ${number(state.pending.length)} waiting`))
      : null,
  );
  const icons = { ok: "✓", failed: "✗", skipped: "–", cancelled: "⊘", deactivated: "!", "rate-limited": "⏸" };
  replaceWith(
    $("#activity-recent"),
    state.recent.slice(0, WAITING_SHOWN).map((job) =>
      h(
        "li",
        { class: `finished ${job.status}` },
        h("span", { class: `run-status ${job.status}` }, icons[job.status] || "?"),
        label(job),
        h("span", { class: "muted" }, job.status === "ok" ? `${number(job.files_new)} new` : job.reason || job.status),
        h("span", { class: "muted" }, sinceShort(job.finished_at)),
        logButton(job),
      )),
  );
  for (const [id, list] of [["#activity-running-block", state.running], ["#activity-pending-block", state.pending], ["#activity-recent-block", state.recent]]) {
    $(id).hidden = list.length === 0;
  }
  // When downloads finish, the numbers below change too
  const finished = state.recent.map((j) => `${j.id}:${j.finished_at}`).join("|");
  if (seenFinished !== null && finished !== seenFinished) {
    loadSummary();
    loadAttention();
    loadHistory({ reset: true, quiet: true });
  }
  seenFinished = finished;
}

const localTime = (timestamp) => new Date(`${timestamp.replace(" ", "T")}Z`).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function tickElapsed() {
  for (const el of document.querySelectorAll("#activity-running .elapsed")) el.textContent = duration(el.dataset.since);
}

async function loadSchedule() {
  try {
    const { schedule } = await api("/status");
    const next = schedule.next_at
      ? new Date(`${schedule.next_at.replace(" ", "T")}Z`).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })
      : "soon";
    replaceWith(
      $("#activity-schedule"),
      schedule.every_hours
        ? `Everything is queued every ${schedule.every_hours} h · next ${schedule.next_at ? `at ${next}` : next} · `
        : "The schedule is off · ",
      h("a", { href: "#settings/run_every_hours" }, schedule.every_hours ? "change" : "turn it on in Settings"),
    );
  } catch {
    $("#activity-schedule").textContent = "";
  }
}

// Summary tiles and the chart

async function loadSummary() {
  try {
    page.stats = await api("/runs/stats?days=30");
  } catch (e) {
    $("#summary-tiles").replaceChildren(h("p", { class: "muted" }, `Could not load the summary: ${e.message}`));
    return;
  }
  renderTotals();
  renderChart(page.stats.per_day);
  const { logs } = page.stats;
  $("#summary-logs").textContent = `Run logs on disk: ${number(logs.files)} file${logs.files === 1 ? "" : "s"}, ${(logs.bytes / 1048576).toFixed(1)} MB`;
}

function renderTotals() {
  if (!page.stats) return;
  const range = $("#summary-range input:checked").value;
  const t = page.stats[range];
  const tile = (value, label, detail, tone) =>
    h("div", { class: `tile-stat${tone ? ` ${tone}` : ""}` }, h("div", { class: "tile-value" }, value), h("div", { class: "tile-label" }, label), detail ? h("div", { class: "tile-detail" }, detail) : null);
  const rate = t.runs ? Math.round((100 * t.ok) / t.runs) : null;
  replaceWith(
    $("#summary-tiles"),
    tile(number(t.files_new), "new files"),
    tile(number(t.runs), "runs", rate === null ? null : `${rate}% succeeded`),
    tile(number(t.failed), "failed runs", null, t.failed ? "warn" : null),
    tile(number(t.files_failed + t.files_retry), "files not fetched", `${number(t.files_retry)} will retry`, t.files_failed + t.files_retry ? "warn" : null),
    tile(number(t.deactivated), "accounts deactivated"),
  );
}

// One series (new files per day), so no legend: the heading names it. The accessible alternative is the table below.
function renderChart(days) {
  const box = $("#chart");
  const width = Math.max(280, box.clientWidth);
  const height = 160;
  const pad = { top: 12, right: 8, bottom: 22, left: 40 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const max = Math.max(1, ...days.map((d) => d.files_new));
  const step = niceStep(max);
  const top = Math.ceil(max / step) * step;
  const slot = plotW / days.length;
  const barW = Math.max(2, slot - 2); // 2px surface gap between bars
  const y = (v) => pad.top + plotH - (v / top) * plotH;
  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}) => {
    const node = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  };
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": "New files per day, last 30 days" });
  for (let v = 0; v <= top; v += step) {
    svg.append(el("line", { x1: pad.left, x2: width - pad.right, y1: y(v), y2: y(v), class: v === 0 ? "axis" : "grid" }));
    const label = el("text", { x: pad.left - 6, y: y(v) + 4, class: "tick", "text-anchor": "end" });
    label.textContent = compact(v);
    svg.append(label);
  }
  days.forEach((d, i) => {
    const x = pad.left + i * slot + 1;
    const h_ = y(0) - y(d.files_new);
    if (d.files_new > 0) {
      // Rounded top, square base anchored to the baseline
      const r = Math.min(4, barW / 2, h_);
      const top_ = y(d.files_new);
      svg.append(el("path", {
        class: "bar",
        d: `M${x},${y(0)} V${top_ + r} Q${x},${top_} ${x + r},${top_} H${x + barW - r} Q${x + barW},${top_} ${x + barW},${top_ + r} V${y(0)} Z`,
      }));
    }
    const hit = el("rect", { x: x - 1, y: pad.top, width: slot, height: plotH, class: "hit", tabindex: "-1" });
    hit.addEventListener("pointerenter", () => showTip(d, x + barW / 2, hit));
    hit.addEventListener("pointerleave", hideTip);
    svg.append(hit);
  });
  for (const i of [0, Math.floor(days.length / 2), days.length - 1]) {
    const label = el("text", { x: pad.left + i * slot + slot / 2, y: height - 6, class: "tick", "text-anchor": i === 0 ? "start" : i === days.length - 1 ? "end" : "middle" });
    label.textContent = shortDate(days[i].day);
    svg.append(label);
  }
  box.replaceChildren(svg, h("div", { class: "chart-tip", id: "chart-tip", hidden: true }));
  // Keyboard: arrow keys move between days
  box.tabIndex = 0;
  box.onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    box.dataset.index = String(Math.min(days.length - 1, Math.max(0, Number(box.dataset.index ?? days.length) + (event.key === "ArrowLeft" ? -1 : 1))));
    const i = Number(box.dataset.index);
    showTip(days[i], pad.left + i * slot + 1 + barW / 2, svg.querySelectorAll(".hit")[i]);
  };
  box.onblur = hideTip;
  replaceWith(
    $("#chart-table tbody"),
    [...days].reverse().map((d) => h("tr", {}, h("td", {}, shortDate(d.day)), h("td", {}, number(d.files_new)), h("td", {}, number(d.runs)), h("td", {}, number(d.failed)))),
  );
}

function showTip(day, x, hit) {
  const tip = $("#chart-tip");
  for (const other of document.querySelectorAll("#chart .hit.active")) other.classList.remove("active");
  hit?.classList.add("active");
  replaceWith(
    tip,
    h("strong", {}, new Date(`${day.day}T12:00:00Z`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })),
    h("div", {}, `${number(day.files_new)} new file${day.files_new === 1 ? "" : "s"}`),
    h("div", { class: "muted" }, `${number(day.runs)} run${day.runs === 1 ? "" : "s"}${day.failed ? ` · ${number(day.failed)} failed` : ""}`),
  );
  tip.hidden = false;
  const box = $("#chart");
  const left = Math.min(Math.max(0, x - tip.offsetWidth / 2), box.clientWidth - tip.offsetWidth);
  tip.style.left = `${left}px`;
}

function hideTip() {
  $("#chart-tip")?.setAttribute("hidden", "");
  for (const other of document.querySelectorAll("#chart .hit.active")) other.classList.remove("active");
}

function niceStep(max) {
  const rough = max / 3;
  const power = 10 ** Math.floor(Math.log10(rough));
  return [1, 2, 5, 10].map((m) => m * power).find((s) => s >= rough) || power * 10;
}

const compact = (v) => (v >= 1000 ? `${(v / 1000).toFixed(v % 1000 ? 1 : 0)}k` : String(v));
const shortDate = (day) => new Date(`${day}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric" });

// Needs attention

async function loadAttention() {
  let data;
  try {
    data = await api("/runs/attention");
  } catch (e) {
    $("#attention-body").replaceChildren(h("p", { class: "muted" }, `Could not load: ${e.message}`));
    return;
  }
  const groups = [];
  if (data.failing_sources.length) {
    groups.push(attentionGroup(
      "Failing repeatedly",
      data.failing_sources.map((s) =>
        h(
          "li",
          {},
          h("a", { class: "job-name", href: redditLink(s.kind, s.name), target: "_blank", rel: "noopener noreferrer" }, sourceLabel(s.kind, s.name)),
          h("span", { class: "muted" }, `${number(s.consecutive_failures)} failures in a row, last ${sinceShort(s.last_run_at)}`),
          h("button", { type: "button", class: "link", onclick: () => queueSource(s) }, "try again"),
          h("button", { type: "button", class: "link", onclick: () => pauseSource(s) }, "pause"),
        )),
    ));
  }
  if (data.failed_runs.length) {
    groups.push(attentionGroup(
      "Failed or interrupted in the last day",
      data.failed_runs.map((run) =>
        h(
          "li",
          {},
          h("span", { class: "run-status failed" }, "✗"),
          h("span", { class: "job-name" }, sourceLabel(run.source_kind, run.source_name)),
          h(
            "span",
            { class: "muted" },
            `${run.interrupted ? "interrupted by a restart · " : ""}${run.rate_limited ? "stopped by a site's rate limit, queued again · " : ""}${sinceShort(run.started_at)}`,
          ),
          runLink(run),
          h("button", { type: "button", class: "link", onclick: () => runAgain(run) }, "run again"),
        )),
    ));
  }
  if (data.file_problems.length) {
    groups.push(attentionGroup(
      "Files that couldn't be downloaded in the last day",
      data.file_problems.map((p) =>
        h(
          "li",
          {},
          h("span", { class: "job-name" }, sourceLabel(p.source_kind, p.source_name)),
          p.files_failed ? h("span", { class: "muted" }, `${number(p.files_failed)} gone for good`) : null,
          p.files_retry ? h("span", { class: "muted" }, `${number(p.files_retry)} will be retried next run`) : null,
          runLink({ id: p.last_run_id, source_kind: p.source_kind, source_name: p.source_name }),
        )),
    ));
  }
  if (data.deactivated.length) {
    groups.push(attentionGroup(
      "Deactivated in the last day",
      data.deactivated.map((d) =>
        h("li", {}, h("span", { class: "run-status deactivated" }, "!"), h("span", { class: "job-name" }, sourceLabel(d.source_kind, d.source_name)), h("span", { class: "muted" }, d.deactivated))),
    ));
  }
  replaceWith($("#attention-body"), groups.length ? groups : h("p", { class: "muted" }, "Nothing needs attention ✓"));
  $("#attention-count").textContent = groups.length ? `${groups.length}` : "";
}

function attentionGroup(title, items) {
  return h("div", { class: "attention-group" }, h("h3", {}, title), h("ul", { class: "job-list" }, items));
}

async function queueSource(source) {
  try {
    const { download: result } = await api(`/sources/${source.kind}/${encodeURIComponent(source.name)}/download`, { method: "POST" });
    toast(queuedMessage(result, sourceLabel(source.kind, source.name)));
    refreshDownloads();
  } catch (e) {
    toast(`Could not queue it: ${e.message}`, { error: true });
  }
}

async function pauseSource(source) {
  try {
    await api(`/sources/${source.kind}/${encodeURIComponent(source.name)}`, { method: "PATCH", body: { status: "paused" } });
    toast(`${sourceLabel(source.kind, source.name)} paused`, {
      undo: async () => {
        await api(`/sources/${source.kind}/${encodeURIComponent(source.name)}`, { method: "PATCH", body: { status: "active" } });
        loadAttention();
      },
    });
    loadAttention();
  } catch (e) {
    toast(`Could not pause it: ${e.message}`, { error: true });
  }
}

// History

function historyFilters() {
  const form = $("#run-filters");
  return {
    status: form.status.value,
    kind: form.kind.value,
    trigger: form.trigger.value,
    since: form.since.value,
    name: form.name.value.trim().replace(/^\/?(u|user|r)\//i, ""),
  };
}

async function loadHistory({ reset, quiet = false }) {
  if (page.loadingHistory) return;
  page.loadingHistory = true;
  const f = historyFilters();
  storage("set", HISTORY_STORAGE, JSON.stringify(f));
  const params = new URLSearchParams({ limit: HISTORY_PAGE });
  for (const key of ["status", "kind", "trigger", "since"]) if (f[key]) params.set(key, f[key]);
  if (f.name) params.set("search", f.name);
  if (!reset && page.oldestId) params.set("before_id", page.oldestId);
  if (!quiet) $("#history-status").textContent = "Loading…";
  try {
    const runs = await api(`/runs?${params}`);
    if (reset) $("#history-list").replaceChildren();
    $("#history-list").append(...runs.map(historyRow));
    page.oldestId = runs.length ? runs[runs.length - 1].id : page.oldestId;
    $("#history-more").hidden = runs.length < HISTORY_PAGE;
    const shown = $("#history-list").children.length;
    $("#history-status").textContent = shown ? "" : "No runs match these filters.";
  } catch (e) {
    $("#history-status").textContent = `Could not load runs: ${e.message}`;
  } finally {
    page.loadingHistory = false;
  }
}

function historyRow(run) {
  const icons = { ok: "✓", failed: "✗", running: "…" };
  const took = run.finished_at ? duration(run.started_at, run.finished_at) : "running";
  const problems = [
    run.files_failed ? `${number(run.files_failed)} failed` : null,
    run.files_retry ? `${number(run.files_retry)} to retry` : null,
  ].filter(Boolean).join(", ");
  return h(
    "li",
    { class: `history-row ${run.status}` },
    h("span", { class: `run-status ${run.status}`, title: run.status }, icons[run.status] || "?"),
    h("span", { class: "history-when", title: new Date(`${run.started_at.replace(" ", "T")}Z`).toLocaleString() }, sinceShort(run.started_at)),
    h("a", { class: "job-name", href: redditLink(run.source_kind, run.source_name), target: "_blank", rel: "noopener noreferrer" }, sourceLabel(run.source_kind, run.source_name)),
    h("span", { class: "chip" }, TRIGGERS[run.trigger] || run.trigger || "unknown"),
    h("span", { class: "muted" }, took),
    h("span", {}, run.files_new === null ? "–" : `${number(run.files_new)} new`),
    problems ? h("span", { class: "muted warn-text" }, problems) : null,
    run.deactivated ? h("span", { class: "muted warn-text" }, "deactivated") : null,
    h("span", { class: "history-actions" }, runLink(run), run.status !== "running" ? h("button", { type: "button", class: "link", onclick: () => runAgain(run) }, "run again") : null),
  );
}
