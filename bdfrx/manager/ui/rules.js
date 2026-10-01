// The Rules page: everything that stops things being downloaded, and how to undo it. Ignored people (with any files
// set aside, to review or restore), skipped subreddits and websites, excluded posts, and dismissed candidates.

import { $, api, confirmIgnore, h, ignoreMessage, ignoreUser, number, redditLink, timeAgo, toast } from "./lib.js";

const state = { rules: [], setAside: new Map(), dismissed: [], userStatus: new Map(), started: false };

const SECTIONS = {
  ignore_user: { title: "Ignored people", empty: "Nobody is ignored." },
  skip_subreddit: {
    title: "Skipped subreddits",
    empty: "No subreddits are skipped.",
    help: "Posts in these subreddits aren't downloaded, whoever posted them.",
    placeholder: "Subreddit, e.g. r/example",
    label: (value) => `r/${value}`,
    link: (value) => redditLink("subreddit", value),
  },
  skip_domain: {
    title: "Skipped websites",
    empty: "No websites are skipped.",
    help: "Files hosted on these sites aren't downloaded.",
    placeholder: "Website, e.g. example.com",
    label: (value) => value,
  },
  exclude_post: {
    title: "Excluded posts",
    empty: "No posts are excluded.",
    help: "These posts are never downloaded.",
    placeholder: "Reddit link or post ID",
    label: (value) => value,
    link: (value) => `https://redd.it/${value}`,
  },
};

export async function showRules() {
  if (!state.started) setUp();
  await load();
}

function setUp() {
  state.started = true;
  $("#rules-search").addEventListener("input", render);
  $("#rules-ignore-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const name = form.name.value.trim().replace(/^\/?(u|user)\//i, "");
    if (!name) return;
    const options = await confirmIgnore(name);
    if (options === null) return;
    try {
      const result = await ignoreUser(name, { ...options, reason: form.reason.value.trim() });
      toast(ignoreMessage(name, result));
      form.reset();
    } catch (e) {
      toast(`Could not ignore u/${name}: ${e.message}`, { error: true });
    }
    load();
  });
}

async function load() {
  try {
    const [rules, setAside, dismissed, users] = await Promise.all([
      api("/rules"),
      api("/set-aside"),
      api("/candidates/dismissed"),
      api("/sources?kind=user"),
    ]);
    state.rules = rules;
    state.setAside = new Map(setAside.map((entry) => [entry.author.toLowerCase(), entry]));
    state.dismissed = dismissed;
    state.userStatus = new Map(users.map((user) => [user.name.toLowerCase(), user.status]));
  } catch (e) {
    toast(`Could not load rules: ${e.message}`, { error: true });
    return;
  }
  render();
}

// replaceChildren would insert missing optional parts as the text "null"
function replaceWith(parent, ...children) {
  parent.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

function matches(text) {
  const find = $("#rules-search").value.trim().toLowerCase().replace(/^\/?(u|user|r)\//, "");
  return !find || text.toLowerCase().includes(find);
}

function render() {
  const byKind = (kind) => state.rules.filter((rule) => rule.kind === kind);
  replaceWith(
    $("#rules-body"),
    ignoredSection(byKind("ignore_user")),
    ...["skip_subreddit", "skip_domain", "exclude_post"].map((kind) => listSection(kind, byKind(kind))),
    dismissedSection(),
  );
}

function sectionShell(title, count, help, ...body) {
  return h(
    "section",
    { class: "panel rules-section" },
    h("div", { class: "panel-head" }, h("h2", {}, title), h("span", { class: "muted" }, number(count))),
    help ? h("p", { class: "muted small" }, help) : null,
    ...body,
  );
}

// Ignored people

function ignoredSection(rules) {
  const shown = rules.filter((rule) => matches(rule.value));
  return sectionShell(
    SECTIONS.ignore_user.title,
    rules.length,
    "Nothing they post is downloaded, anywhere. Files set aside when you ignored them wait in the archive's ignored folder to be reviewed, restored or deleted by hand.",
    h("ul", { class: "job-list rules-list" }, shown.length ? shown.map(ignoredRow) : h("li", { class: "muted" }, rules.length ? "No matches." : SECTIONS.ignore_user.empty)),
  );
}

function ignoredRow(rule) {
  const name = rule.value;
  const aside = state.setAside.get(name.toLowerCase());
  const status = state.userStatus.get(name.toLowerCase());
  return h(
    "li",
    {},
    h("a", { class: "job-name", href: redditLink("user", name), target: "_blank", rel: "noopener noreferrer" }, `u/${name}`),
    status ? h("span", { class: `pill status-${status}` }, status === "paused" ? "following, paused" : status) : null,
    h("span", { class: "muted" }, `since ${timeAgo(rule.created_at)}`),
    rule.reason ? h("span", { class: "muted rule-reason" }, `“${rule.reason}”`) : null,
    aside
      ? h(
          "span",
          { class: "muted" },
          `${number(aside.files)} file${aside.files === 1 ? "" : "s"} set aside · `,
          h("a", { href: `#gallery/ignored/${encodeURIComponent(name)}` }, "review"),
        )
      : null,
    h(
      "span",
      { class: "rule-actions" },
      aside ? h("button", { type: "button", class: "primary", onclick: () => unignore(name, { restore: true }) }, "Unignore & restore files") : null,
      h("button", { type: "button", onclick: () => unignore(name, { restore: false }) }, aside ? "Unignore only" : "Unignore"),
    ),
  );
}

async function unignore(name, { restore }) {
  const reason = state.rules.find((rule) => rule.kind === "ignore_user" && rule.value === name)?.reason;
  let restored = null;
  try {
    if (restore) restored = await api(`/set-aside/${encodeURIComponent(name)}`, { method: "DELETE" });
    await api(`/rules/ignore_user/${encodeURIComponent(name)}`, { method: "DELETE" });
  } catch (e) {
    toast(`Could not unignore u/${name}: ${e.message}`, { error: true });
    load();
    return;
  }
  const parts = [`No longer ignoring u/${name}`];
  if (restored) {
    parts.push(`${number(restored.moved)} file${restored.moved === 1 ? "" : "s"} restored`);
    if (restored.skipped.length) parts.push(`${number(restored.skipped.length)} not restored (deleted or already back)`);
  }
  if (state.userStatus.get(name.toLowerCase()) === "paused") parts.push("still paused in Sources");
  toast(parts.join(" · "), {
    undo: async () => {
      await ignoreUser(name, { setAside: restore, reason });
      load();
    },
  });
  load();
}

// Skipped subreddits, skipped websites, excluded posts

function listSection(kind, rules) {
  const section = SECTIONS[kind];
  const shown = rules.filter((rule) => matches(rule.value) || matches(rule.reason || ""));
  const input = h("input", { name: "value", placeholder: section.placeholder, autocomplete: "off", spellcheck: "false", required: true });
  const reason = h("input", { name: "reason", placeholder: "Why (optional)", autocomplete: "off" });
  const form = h(
    "form",
    { class: "rules-add" },
    input,
    reason,
    h("button", { type: "submit", class: "primary" }, "Add"),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const rule = await api("/rules", { method: "POST", body: { kind, value: input.value.trim(), reason: reason.value.trim() || null } });
      toast(`Added ${section.label(rule.value)}`, {
        undo: async () => {
          await api(`/rules/${kind}/${encodeURIComponent(rule.value)}`, { method: "DELETE" });
          load();
        },
      });
      load();
    } catch (e) {
      toast(`Not added: ${e.message}`, { error: true });
    }
  });
  return sectionShell(
    section.title,
    rules.length,
    section.help,
    form,
    h(
      "ul",
      { class: "job-list rules-list" },
      shown.length
        ? shown.map((rule) =>
            h(
              "li",
              {},
              section.link
                ? h("a", { class: "job-name", href: section.link(rule.value), target: "_blank", rel: "noopener noreferrer" }, section.label(rule.value))
                : h("span", { class: "job-name" }, section.label(rule.value)),
              h("span", { class: "muted" }, `since ${timeAgo(rule.created_at)}`),
              rule.reason ? h("span", { class: "muted rule-reason" }, `“${rule.reason}”`) : null,
              h("span", { class: "rule-actions" }, h("button", { type: "button", onclick: () => removeRule(rule) }, "Remove")),
            ))
        : h("li", { class: "muted" }, rules.length ? "No matches." : section.empty),
    ),
  );
}

async function removeRule(rule) {
  const label = SECTIONS[rule.kind].label(rule.value);
  try {
    await api(`/rules/${rule.kind}/${encodeURIComponent(rule.value)}`, { method: "DELETE" });
  } catch (e) {
    toast(`Could not remove ${label}: ${e.message}`, { error: true });
    return;
  }
  toast(`Removed ${label}`, {
    undo: async () => {
      await api("/rules", { method: "POST", body: { kind: rule.kind, value: rule.value, reason: rule.reason } });
      load();
    },
  });
  load();
}

// Dismissed candidates

function dismissedSection() {
  const shown = state.dismissed.filter((entry) => matches(entry.name));
  return sectionShell(
    "Dismissed candidates",
    state.dismissed.length,
    "Hidden from Candidates; their posts still download from the subreddits you follow.",
    h(
      "ul",
      { class: "job-list rules-list" },
      shown.length
        ? shown.map((entry) =>
            h(
              "li",
              {},
              h("a", { class: "job-name", href: redditLink("user", entry.name), target: "_blank", rel: "noopener noreferrer" }, `u/${entry.name}`),
              h("span", { class: "muted" }, `since ${timeAgo(entry.dismissed_at)}`),
              entry.reason ? h("span", { class: "muted rule-reason" }, `“${entry.reason}”`) : null,
              h("span", { class: "rule-actions" }, h("button", { type: "button", onclick: () => undismiss(entry) }, "Undismiss")),
            ))
        : h("li", { class: "muted" }, state.dismissed.length ? "No matches." : "Nobody is dismissed."),
    ),
  );
}

async function undismiss(entry) {
  try {
    await api(`/candidates/${encodeURIComponent(entry.name)}/dismissed`, { method: "DELETE" });
  } catch (e) {
    toast(`Could not undismiss u/${entry.name}: ${e.message}`, { error: true });
    return;
  }
  toast(`u/${entry.name} is a candidate again`, {
    undo: async () => {
      await api(`/candidates/${encodeURIComponent(entry.name)}/dismissed`, { method: "PUT", body: { reason: entry.reason } });
      load();
    },
  });
  load();
}
