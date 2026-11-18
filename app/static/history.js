// 历史记录：列出以前的运行，支持按关键词和状态筛选，点开回看。

import { $, el, pad, timeLabel } from "./util.js";
import { STATUS } from "./state.js";
import { openRun } from "./events.js";

let cachedRuns = [];
let currentFilter = "all";

function renderHistory() {
  const box = $("history");
  const term = ($("history-search")?.value || "").trim().toLowerCase();
  const filtered = cachedRuns.filter((run) => {
    if (currentFilter === "done" && run.status !== "COMPLETE") return false;
    if (currentFilter === "other" && run.status === "COMPLETE") return false;
    if (term && !(run.query || "").toLowerCase().includes(term)) return false;
    return true;
  });

  const count = $("history-count");
  if (count) {
    count.textContent = cachedRuns.length
      ? (filtered.length === cachedRuns.length ? `共 ${cachedRuns.length} 条` : `${filtered.length} / ${cachedRuns.length} 条`)
      : "";
  }

  box.replaceChildren();
  if (!cachedRuns.length) {
    box.append(el("div", "row-empty", "还没有运行记录"));
    return;
  }
  if (!filtered.length) {
    box.append(el("div", "row-empty", "没有匹配的历史任务"));
    return;
  }

  filtered.forEach((run, i) => {
    const [label, cls] = STATUS[run.status] || STATUS.UNKNOWN;
    const row = el("button", "row");
    row.type = "button";
    const side = el("span", "row-side");
    side.append(el("span", "state " + cls, label));
    const t = timeLabel(run.time);
    if (t) side.append(el("span", null, " · " + t));
    row.append(
      el("span", "num", pad(i + 1)),
      el("span", "row-title", run.query || "（无标题）"),
      side,
    );
    row.addEventListener("click", () => openRun(run.id, run.status === "RUNNING"));
    box.append(row);
  });
}

export function initHistory() {
  $("history-search")?.addEventListener("input", renderHistory);
  for (const b of document.querySelectorAll("#history-filter button")) {
    b.addEventListener("click", () => {
      currentFilter = b.dataset.filter || "all";
      for (const tab of document.querySelectorAll("#history-filter button")) {
        tab.setAttribute("aria-checked", String(tab === b));
      }
      renderHistory();
    });
  }
}

export function refreshHistory() {
  fetch("/api/runs")
    .then((r) => r.json())
    .then((runs) => {
      cachedRuns = runs || [];
      renderHistory();
    });
}
