// A source's gallery (#gallery/u/name, #gallery/r/name): everything in its archive folder as a grid of thumbnails,
// newest post first, with gallery posts grouped. Tapping one opens the full-screen viewer.

import { $, api, h, number, redditLink, sourceLabel, storage } from "./lib.js";
import { openViewer, viewerItems } from "./viewer.js";

const PAGE_SIZE = 60;
const FILTER_STORAGE = "bdfrx_manager_gallery_filters";

const page = { started: false, source: null, offset: 0, total: 0, done: false, loading: false, generation: 0, posts: [] };

export function showGallery(path = []) {
  if (!page.started) setUp();
  const [prefix, name] = path;
  if (!name || (prefix !== "u" && prefix !== "r")) {
    location.hash = "#sources";
    return;
  }
  page.source = { kind: prefix === "u" ? "user" : "subreddit", name };
  document.title = `${sourceLabel(page.source.kind, name)} · gallery · bdfrx manager`;
  reset();
}

function setUp() {
  page.started = true;
  const form = $("#gallery-filters");
  try {
    const saved = JSON.parse(storage("get", FILTER_STORAGE) || "{}");
    for (const [name, value] of Object.entries(saved)) form.querySelector(`input[name="${name}"][value="${value}"]`)?.click();
  } catch {
    // ignore unreadable saved filters
  }
  form.addEventListener("change", () => {
    storage("set", FILTER_STORAGE, JSON.stringify(filters()));
    reset();
  });
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadMore();
  }, { rootMargin: "1000px" }).observe($("#gallery-sentinel"));
}

function filters() {
  const form = $("#gallery-filters");
  return {
    media: form.querySelector("input[name=media]:checked").value,
    order: form.querySelector("input[name=order]:checked").value,
  };
}

function reset() {
  page.generation += 1;
  page.offset = 0;
  page.total = 0;
  page.done = false;
  page.loading = false;
  page.posts = [];
  $("#gallery-grid").replaceChildren();
  const { kind, name } = page.source;
  $("#gallery-title").replaceChildren(
    h("a", { href: redditLink(kind, name), target: "_blank", rel: "noopener noreferrer" }, sourceLabel(kind, name)),
  );
  $("#gallery-count").textContent = "";
  $("#gallery-latest").setAttribute("href", `#latest/${kind === "user" ? "u" : "r"}/${encodeURIComponent(name)}`);
  loadMore();
}

async function loadMore() {
  if (page.loading || page.done || !page.source) return;
  page.loading = true;
  const generation = page.generation;
  const { kind, name } = page.source;
  const params = new URLSearchParams({ offset: page.offset, limit: PAGE_SIZE, ...filters() });
  $("#gallery-status").textContent = "Loading…";
  try {
    const result = await api(`/sources/${kind}/${encodeURIComponent(name)}/gallery?${params}`);
    if (generation !== page.generation) return;
    page.offset += result.posts.length;
    page.total = result.total_posts;
    page.done = page.offset >= result.total_posts;
    page.posts.push(...result.posts);
    $("#gallery-grid").append(...result.posts.map(tile));
    $("#gallery-count").textContent = `${number(result.total_posts)} post${result.total_posts === 1 ? "" : "s"} · ${number(result.total_files)} file${result.total_files === 1 ? "" : "s"}`;
    $("#gallery-status").textContent = page.done && !page.total ? "Nothing in this source's folder yet." : "";
  } catch (e) {
    if (generation === page.generation) $("#gallery-status").textContent = `Could not load: ${e.message}`;
  } finally {
    if (generation === page.generation) page.loading = false;
  }
  if (!page.done && generation === page.generation && $("#gallery-sentinel").getBoundingClientRect().top < window.innerHeight + 1000) {
    loadMore();
  }
}

function tile(post) {
  const first = post.files[0];
  const caption = page.source.kind === "user" ? (post.subreddit ? `r/${post.subreddit}` : first.name) : post.author ? `u/${post.author}` : first.name;
  const button = h("button", {
    type: "button",
    class: "gallery-tile",
    title: caption,
    onclick: () => {
      // The viewer steps through every file loaded so far, starting at this post
      const items = viewerItems(page.posts.map(asViewerPost));
      openViewer(items, Math.max(0, items.findIndex((item) => item.post.key === key(post))));
    },
  });
  const box = h("span", { class: "thumb-box" });
  if (first.kind === "image" || first.kind === "video") {
    const img = h("img", { src: `${first.url}/thumbnail`, loading: "lazy", alt: "", decoding: "async" });
    img.addEventListener("load", () => box.classList.add("loaded"), { once: true });
    img.addEventListener("error", () => img.remove(), { once: true });
    box.append(img);
    if (first.kind === "video") box.append(h("span", { class: "play" }, "▶"));
  } else {
    box.append(h("span", { class: "initial" }, first.kind === "audio" ? "♪" : first.kind === "text" ? "¶" : "▢"));
  }
  if (post.files.length > 1) box.append(h("span", { class: "badge" }, `1/${post.files.length}`));
  button.append(box);
  return button;
}

const key = (post) => post.post_id || post.files[0].name;

// The viewer expects feed-style posts; archive files have no recorded post date
const viewerPosts = new Map();
function asViewerPost(post) {
  if (!viewerPosts.has(post)) {
    viewerPosts.set(post, {
      key: key(post),
      post_id: post.post_id || "",
      subreddit: post.subreddit || page.source.name,
      score: post.score,
      posted_at: null,
      files: post.files,
    });
  }
  return viewerPosts.get(post);
}
