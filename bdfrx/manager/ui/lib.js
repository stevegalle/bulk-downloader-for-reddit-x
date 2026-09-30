// Shared helpers for the bdfrx manager UI: DOM building, the API client (with the key), toasts, formatting.

const KEY_STORAGE = "bdfrx_manager_key";
const KEY_COOKIE = "bdfrx_manager_key";
const SET_ASIDE_STORAGE = "bdfrx_manager_set_aside_on_ignore";

export const $ = (selector, root = document) => root.querySelector(selector);

// Build an element: h("a", {href: "#", class: "x"}, "text", childElement). Text is never parsed as HTML.
export function h(tag, attrs = {}, ...children) {
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

export function storage(action, key, value) {
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

export function getKey() {
  return storage("get", KEY_STORAGE) || "";
}

export function setKey(key) {
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

export function askForKey(message) {
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

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = "GET", body, retry = true } = {}) {
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

export function toast(message, { error = false, undo } = {}) {
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

export function timeAgo(timestamp) {
  if (!timestamp) return "unknown";
  const then = new Date(`${timestamp.replace(" ", "T")}Z`);
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days < 1) return "today";
  if (days === 1) return "yesterday";
  if (days < 45) return `${days} days ago`;
  if (days < 540) return `${Math.round(days / 30)} months ago`;
  return `${(days / 365).toFixed(1)} years ago`;
}

export const number = (n) => (n ?? 0).toLocaleString();

// Finer-grained than timeAgo, for runs and queue entries: "just now", "12 min ago", "3 h ago", then days
export function sinceShort(timestamp) {
  if (!timestamp) return "never";
  const seconds = (Date.now() - new Date(`${timestamp.replace(" ", "T")}Z`)) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return timeAgo(timestamp);
}

// Time from one UTC timestamp to another (or to now): "42s", "3m 05s", "1h 12m"
export function duration(fromTimestamp, toTimestamp) {
  const end = toTimestamp ? new Date(`${toTimestamp.replace(" ", "T")}Z`) : Date.now();
  const seconds = Math.max(0, Math.floor((end - new Date(`${fromTimestamp.replace(" ", "T")}Z`)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(seconds / 3600)}h ${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}m`;
}

export const redditLink = (kind, name) =>
  kind === "user" ? `https://www.reddit.com/user/${encodeURIComponent(name)}/submitted/` : `https://www.reddit.com/r/${encodeURIComponent(name)}/`;

export const sourceLabel = (kind, name) => `${kind === "user" ? "u/" : "r/"}${name}`;

// What happened when a download was asked for: queued, moved to the front of the queue, or already queued
export function queuedMessage(result, label, detail = "") {
  if (result === "queued") return `Queued ${label}${detail}`;
  if (result === "moved up") return `Moved ${label} to the front of the queue${detail}`;
  return `${label} is already queued or downloading`;
}

// Ignoring a user, shared by Candidates and Sources: asks first, optionally moving their files aside

export async function confirmIgnore(name) {
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

export async function ignoreUser(name, { setAside }) {
  await api("/rules", { method: "POST", body: { kind: "ignore_user", value: name } });
  if (!setAside) return { moved: 0, skipped: [] };
  return api(`/set-aside/${encodeURIComponent(name)}`, { method: "POST" });
}

export async function unignoreUser(name, { setAside }) {
  if (setAside) await api(`/set-aside/${encodeURIComponent(name)}`, { method: "DELETE" });
  await api(`/rules/ignore_user/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export function ignoreMessage(name, result) {
  if (!result || !result.moved) return `Ignoring u/${name} everywhere`;
  const skipped = result.skipped.length ? `, ${result.skipped.length} skipped` : "";
  return `Ignoring u/${name} · moved ${number(result.moved)} file${result.moved === 1 ? "" : "s"} aside${skipped}`;
}
