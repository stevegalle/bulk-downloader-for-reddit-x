// The run log viewer, shared by the Sources and Runs pages. Follows a running download live, and marks warnings and
// errors so problems are easy to spot.

import { $, api, number, sourceLabel } from "./lib.js";

// Log viewer, following a running download

const log = { runId: null, timer: null };

export function setUpLogDialog() {
  const dialog = $("#log-dialog");
  $("#log-close").addEventListener("click", () => dialog.close());
  $("#log-next").addEventListener("click", nextProblem);
  dialog.addEventListener("close", () => {
    clearTimeout(log.timer);
    log.runId = null;
  });
}

export async function openLog(run) {
  const dialog = $("#log-dialog");
  log.runId = run.id;
  $("#log-title").textContent = `${sourceLabel(run.source_kind, run.source_name)} · run ${run.id}`;
  $("#log-text").textContent = "Loading…";
  if (!dialog.open) dialog.showModal();
  followLog(run.id);
}

async function followLog(runId) {
  clearTimeout(log.timer);
  if (log.runId !== runId) return;
  const pre = $("#log-text");
  try {
    const [text, run] = await Promise.all([api(`/runs/${runId}/log`), api(`/runs/${runId}`)]);
    if (log.runId !== runId) return;
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40;
    showLog(pre, text || "(no output yet)");
    if (atBottom) pre.scrollTop = pre.scrollHeight;
    $("#log-status").textContent = run.status === "running" ? "running · updating live" : `${run.status} · ${number(run.files_new)} new file(s)`;
    if (run.status === "running") log.timer = setTimeout(() => followLog(runId), 3000);
  } catch (e) {
    pre.textContent = `Could not load the log: ${e.message}`;
  }
}


// Colour warning and error lines, and let "next problem" jump between them
function showLog(pre, text) {
  pre.replaceChildren(
    ...text.split("\n").map((line) => {
      const level = / - (ERROR|CRITICAL)\] - |Traceback|^\[manager\]/.test(line) ? "error" : / - WARNING\] - /.test(line) ? "warning" : "";
      const span = document.createElement("span");
      if (level) span.className = `log-${level}`;
      span.textContent = `${line}\n`;
      return span;
    }),
  );
  const problems = pre.querySelectorAll(".log-error, .log-warning").length;
  $("#log-next").hidden = problems === 0;
  $("#log-next").textContent = `Next problem (${number(problems)})`;
}

let problemIndex = -1;

export function nextProblem() {
  const problems = [...$("#log-text").querySelectorAll(".log-error, .log-warning")];
  if (!problems.length) return;
  problemIndex = (problemIndex + 1) % problems.length;
  problems[problemIndex].scrollIntoView({ block: "center" });
  for (const p of problems) p.classList.remove("current");
  problems[problemIndex].classList.add("current");
}
