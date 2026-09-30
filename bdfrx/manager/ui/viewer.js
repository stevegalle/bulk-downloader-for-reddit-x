// The full-screen viewer for a set of posts' files: images, videos, audio and text, with swipe and arrow keys.

import { $, h, number, timeAgo } from "./lib.js";

// Full-screen viewer

const viewer = { items: [], index: 0 };

export function openViewer(items, index) {
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

export function setUpViewer() {
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


// Every file of every post, in order, for stepping through in the viewer
export function viewerItems(posts) {
  return posts.flatMap((post) => post.files.map((file) => ({ post, file })));
}
