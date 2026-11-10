// 历史记录：列出以前的运行，点开回看。

import { $, el, pad, timeLabel } from "./util.js";
import { STATUS } from "./state.js";
import { openRun } from "./events.js";

export function refreshHistory() {
  fetch("/api/runs")
    .then((r) => r.json())
    .then((runs) => {
      const box = $("history");
      box.replaceChildren();
      if (!runs.length) {
        box.append(el("div", "row-empty", "还没有运行记录"));
        return;
      }
      runs.forEach((run, i) => {
        const [label] = STATUS[run.status] || STATUS.UNKNOWN;
        const row = el("button", "row");
        row.type = "button";
        row.append(
          el("span", "num", pad(i + 1)),
          el("span", "row-title", run.query || "（无标题）"),
          el("span", "row-side", `${label} · ${timeLabel(run.time)}`)
        );
        row.addEventListener("click", () => openRun(run.id, run.status === "RUNNING"));
        box.append(row);
      });
    });
}
