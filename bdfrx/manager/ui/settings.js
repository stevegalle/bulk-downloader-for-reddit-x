// The Settings page: the manager's settings in plain language, grouped, each saved as soon as it's changed. Settings
// that name programs, paths or file naming are shown but locked: the server only lets them change on the Pi itself.

import { $, api, h, setKey, storage, toast } from "./lib.js";

const TIME_CHOICES = [
  ["all", "As far back as Reddit lists"],
  ["year", "The last year"],
  ["month", "The last month"],
  ["week", "The last week"],
  ["day", "The last day"],
  ["hour", "The last hour"],
];

// How each setting is shown. Anything the server has that isn't here appears under "Other" with its own help.
const SECTIONS = [
  {
    title: "Downloading",
    fields: [
      {
        key: "run_every_hours",
        label: "Download everything automatically",
        help: "Every active source is queued this often, least recently run first.",
        type: "select",
        choices: [["0", "Off"], ...[1, 2, 3, 4, 6, 8, 12, 24].map((n) => [String(n), n === 1 ? "Every hour" : `Every ${n} hours`])],
      },
      {
        key: "max_concurrent_downloads",
        label: "Downloads at once",
        help: "They all use your one Reddit account, so keep this small.",
        type: "number",
        min: 1,
        max: 8,
      },
      {
        key: "incremental_runs",
        label: "Only read what's new",
        help: "Each run reads back to the source's last successful run instead of everything Reddit lists. First runs and all-time downloads still read everything.",
        type: "toggle",
      },
      {
        key: "incremental_overlap_hours",
        label: "Overlap with the last run",
        help: "Hours before the last successful run to start reading, to catch posts that show up late.",
        type: "number",
        min: 0,
        unit: "hours",
      },
      {
        key: "follow_download_time",
        label: "When you follow someone, download",
        help: "Also used when adding a source with \"download their history now\".",
        type: "select",
        choices: TIME_CHOICES,
      },
      {
        key: "rate_limit_backoff_minutes",
        label: "After a site's rate limit, wait",
        help: "A download stopped by a site refusing too many requests goes to the back of the queue for this long.",
        type: "number",
        min: 1,
        unit: "minutes",
      },
      {
        key: "run_timeout",
        label: "Stop a single download after",
        help: "A run that takes longer than this is stopped (an all-time download of a busy source can take a while).",
        type: "number",
        min: 60,
        unit: "seconds",
      },
    ],
  },
  {
    title: "Defaults for sources",
    note: "Each source can override these in its Edit panel.",
    fields: [
      {
        key: "default_sort",
        label: "Sort",
        help: "\"New\" works best: with it, runs can stop as soon as they reach posts already seen.",
        type: "select",
        choices: ["new", "hot", "top", "controversial", "rising", "relevance"].map((v) => [v, v[0].toUpperCase() + v.slice(1)]),
      },
      {
        key: "default_time",
        label: "Time window",
        help: "Only used with the Top and Controversial sorts.",
        type: "select",
        choices: TIME_CHOICES,
      },
      {
        key: "default_filter_gender",
        label: "Skip posts tagged for",
        help: "Uses the [M4F]-style tags in post titles.",
        type: "select",
        choices: [["male", "Men (skip [M4…] posts)"], ["female", "Women (skip [F4…] posts)"], ["none", "Don't filter"]],
      },
    ],
  },
  {
    title: "Housekeeping",
    fields: [
      {
        key: "log_retention_days",
        label: "Keep run logs for",
        help: "Each run's result and last lines stay for good; only the full log file is deleted.",
        type: "number",
        min: 1,
        unit: "days",
      },
    ],
  },
  {
    title: "Storage and commands",
    note: "Locked here, because they name programs and folders on the Pi. Change them there with: bdfrx-manager config set <name> <value>",
    fields: [
      { key: "dest_root", label: "Archive folder" },
      { key: "staging_root", label: "Staging folder", help: "Downloads land here first, then move into the archive. Empty: straight into the archive." },
      { key: "ignored_folder", label: "Set-aside folder", help: "Inside the archive, for files of people you ignore." },
      { key: "history_db_user", label: "User history" },
      { key: "history_db_subreddit", label: "Subreddit history" },
      { key: "log_dir", label: "Logs" },
      { key: "bdfrx_command", label: "bdfrx command" },
      { key: "bdfrx_config", label: "bdfrx config" },
      { key: "file_scheme", label: "File names" },
      { key: "folder_scheme", label: "Folder names" },
      { key: "authenticate", label: "Sign in to Reddit" },
    ],
  },
];

let settings = {};

export async function showSettings(path = []) {
  try {
    settings = await api("/settings");
  } catch (e) {
    $("#settings-body").replaceChildren(h("p", { class: "muted" }, `Could not load settings: ${e.message}`));
    return;
  }
  render();
  // "#settings/<name>" scrolls to that setting and highlights it
  const target = path[0] && document.getElementById(`setting-${path[0]}`);
  if (target) {
    target.scrollIntoView({ block: "center" });
    target.classList.add("flash");
    setTimeout(() => target.classList.remove("flash"), 1600);
  }
}

function render() {
  const shown = new Set(SECTIONS.flatMap((s) => s.fields.map((f) => f.key)));
  const others = Object.keys(settings).filter((key) => !shown.has(key)).map((key) => ({ key, label: key }));
  const sections = others.length ? [...SECTIONS, { title: "Other", fields: others }] : SECTIONS;
  $("#settings-body").replaceChildren(
    ...sections.map((section) =>
      h(
        "section",
        { class: "panel settings-section" },
        h("h2", {}, section.title),
        section.note ? h("p", { class: "muted small" }, section.note) : null,
        h("div", { class: "settings-list" }, section.fields.filter((f) => settings[f.key]).map(settingRow)),
      )),
    deviceSection(),
  );
}

function settingRow(field) {
  const setting = settings[field.key];
  const row = h("div", { class: "setting", id: `setting-${field.key}` });
  const id = `input-${field.key}`;
  const changed = setting.value !== setting.default;
  let control;
  if (!setting.editable) {
    control = h("code", { class: "setting-locked" }, setting.value || "(empty)");
  } else if (field.type === "select") {
    const choices = [...field.choices];
    if (!choices.some(([value]) => value === setting.value)) choices.push([setting.value, setting.value]); // e.g. "0.5" hours
    control = h("select", { id }, choices.map(([value, text]) => h("option", { value, selected: value === setting.value }, text)));
  } else if (field.type === "toggle") {
    control = h("label", { class: "switch" }, h("input", { id, type: "checkbox", checked: setting.value === "true", role: "switch" }), h("span", { "aria-hidden": "true" }));
  } else if (field.type === "number") {
    control = h(
      "span",
      { class: "number-with-unit" },
      h("input", { id, type: "number", inputmode: "numeric", min: field.min, max: field.max, value: setting.value }),
      field.unit ? h("span", { class: "muted" }, field.unit) : null,
    );
  } else {
    control = h("input", { id, type: "text", value: setting.value, spellcheck: "false" });
  }
  const input = control.matches?.("input, select") ? control : control.querySelector?.("input, select");
  if (input) input.addEventListener("change", () => save(field, input.type === "checkbox" ? String(input.checked) : input.value.trim()));
  row.append(
    h(
      "div",
      { class: "setting-text" },
      h("label", { for: setting.editable ? id : null, class: "setting-label" }, field.label),
      h("p", { class: "muted small" }, field.help || setting.help),
      changed && setting.editable
        ? h("button", { type: "button", class: "link small", onclick: () => save(field, null) }, `Reset to default (${describe(field, setting.default)})`)
        : null,
    ),
    h("div", { class: "setting-control" }, control),
  );
  return row;
}

function describe(field, value) {
  if (field.type === "select") return field.choices.find(([v]) => v === value)?.[1] || value;
  if (field.type === "toggle") return value === "true" ? "on" : "off";
  return field.unit ? `${value} ${field.unit}` : value || "empty";
}

async function save(field, value) {
  const before = settings[field.key].value;
  try {
    const result = await api(`/settings/${field.key}`, { method: "PUT", body: { value } });
    settings[field.key].value = result[field.key];
    toast(`${field.label}: ${describe(field, result[field.key])}`, {
      undo: async () => {
        await api(`/settings/${field.key}`, { method: "PUT", body: { value: before } });
        settings[field.key].value = before;
        render();
      },
    });
  } catch (e) {
    toast(`Not saved: ${e.message}`, { error: true });
  }
  render();
}

function deviceSection() {
  return h(
    "section",
    { class: "panel settings-section" },
    h("h2", {}, "This device"),
    h(
      "div",
      { class: "settings-list" },
      h(
        "div",
        { class: "setting" },
        h("div", { class: "setting-text" }, h("span", { class: "setting-label" }, "API key"), h("p", { class: "muted small" }, "Remembered in this browser only. Forget it on shared devices.")),
        h(
          "div",
          { class: "setting-control" },
          h("button", {
            type: "button",
            class: "danger",
            onclick: () => {
              setKey("");
              storage("remove", "bdfrx_manager_key");
              toast("Key forgotten on this device; you'll be asked for it again");
            },
          }, "Forget key"),
        ),
      ),
    ),
  );
}
