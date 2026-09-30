// The download queue as the UI sees it: polled from /downloads, shown as a pill in the top bar, reported with a toast
// as each download finishes, and handed to anything that subscribes (the Sources page's activity panel).

import { $, api, number, sourceLabel, toast } from "./lib.js";

const BUSY_POLL = 3000;
const IDLE_POLL = 15000;

let timer = null;
let state = { running: [], pending: [], recent: [], slots: 2 };
let reported = null; // ids of finished jobs already announced; null until the first poll
let watchers = 0; // pages that want updates even when idle
const listeners = new Set();

export function currentState() {
  return state;
}

export function subscribe(listener) {
  listeners.add(listener);
  listener(state);
  return () => listeners.delete(listener);
}

// Pages showing the queue keep polling while idle, so work started elsewhere (the schedule, the CLI) appears
export function keepWatching(on) {
  watchers = Math.max(0, watchers + (on ? 1 : -1));
  if (on) refresh();
}

function recentKey(job) {
  return `${job.id}:${job.finished_at}`;
}

function announce(job) {
  const label = sourceLabel(job.kind, job.name);
  if (job.status === "ok") toast(`${label}: ${number(job.files_new)} new file${job.files_new === 1 ? "" : "s"}`);
  else if (job.status === "deactivated") toast(`${label} deactivated: ${job.reason}`, { error: true });
  else if (job.status === "skipped") toast(`${label}: download skipped (${job.reason})`);
  else if (job.status === "failed") toast(`${label}: download failed${job.reason ? ` (${job.reason})` : ""}. See its log.`, { error: true });
}

function renderPill() {
  const pill = $("#activity");
  const busy = state.running.length || state.pending.length;
  if (!busy) {
    pill.hidden = true;
    return;
  }
  const running = state.running.map((j) => sourceLabel(j.kind, j.name));
  const now = running.length ? `Downloading ${running.join(", ")}` : "Starting downloads";
  pill.textContent = now + (state.pending.length ? ` · ${state.pending.length} queued` : "");
  pill.title = [...running, ...state.pending.map((j) => sourceLabel(j.kind, j.name))].join(", ");
  pill.hidden = false;
}

export async function refresh() {
  clearTimeout(timer);
  try {
    state = await api("/downloads");
  } catch {
    timer = setTimeout(refresh, IDLE_POLL);
    return;
  }
  // Announce downloads that finished since the last poll (not cancellations, and not on the first poll)
  const keys = new Set(state.recent.map(recentKey));
  if (reported !== null) {
    for (const job of [...state.recent].reverse()) if (!reported.has(recentKey(job)) && job.status !== "cancelled") announce(job);
  }
  reported = keys;
  renderPill();
  for (const listener of listeners) listener(state);
  const busy = state.running.length || state.pending.length;
  if (busy || watchers) timer = setTimeout(refresh, busy ? BUSY_POLL : IDLE_POLL);
}

export async function cancel(jobId) {
  await api(jobId === undefined ? "/downloads" : `/downloads/${jobId}`, { method: "DELETE" });
  refresh();
}
