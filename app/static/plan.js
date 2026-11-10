// 左边的“计划”：显示、确认前修改、运行中修改、逐项进度。

import { $, el, pad, pill } from "./util.js";
import { state } from "./state.js";
import { postDecision, setStatus, updateSendButton } from "./ui.js";
import { addNote } from "./log.js";

export function setPlan(steps, source) {
  const p = state.view.plan;
  // 改过的计划：同一位置、同样文字的步骤保留原来的进度
  const progress = steps.map((s, i) => (p.steps[i] === s ? p.state[i] : "pending"));
  const notes = steps.map((s, i) => (p.steps[i] === s ? p.notes[i] : ""));
  Object.assign(p, { steps, state: progress, notes, source, pendingEdit: false });
  if (source === "edited") addNote("计划已按你的修改更新");
  $("plan-block").hidden = false;
  renderPlan();
}

export function planProgress(e) {
  const p = state.view.plan;
  const i = e.step - 1;
  if (i < 0 || i >= p.steps.length) return;
  if (e.status === "in_progress") {
    // 同一时间只有一项在进行
    p.state = p.state.map((s) => (s === "in_progress" ? "pending" : s));
  }
  p.state[i] = { in_progress: "in_progress", done: "done", failed: "failed" }[e.status] || p.state[i];
  if (e.note) p.notes[i] = e.note;
  if (!p.editing) renderPlan();
}

export function planAwaiting() {
  const view = state.view;
  const p = view.plan;
  p.awaiting = true;
  if (view.live && view.running) {
    setStatus("WAITING");
    p.editing = true;
    p.draft = [...p.steps];
  }
  updateSendButton();
  renderPlan();
}

export function planDecided(e) {
  const view = state.view;
  const p = view.plan;
  p.awaiting = false;
  p.editing = false;
  if (e.value !== "continue") addNote("你取消了这个计划");
  else if (view.running) setStatus("RUNNING");
  updateSendButton();
  renderPlan();
}

export function renderPlan() {
  const view = state.view;
  const p = view.plan;
  const box = $("plan");
  const actions = $("plan-actions");
  box.replaceChildren();
  actions.replaceChildren();

  if (p.editing) {
    p.draft.forEach((text, i) => {
      const row = el("div", "step");
      const input = el("input");
      input.value = text;
      input.placeholder = "写下这一步要做什么";
      input.addEventListener("input", () => (p.draft[i] = input.value));
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !ev.isComposing) {
          ev.preventDefault();
          p.draft.splice(i + 1, 0, "");
          renderPlan();
          box.children[i + 1]?.querySelector("input")?.focus();
        }
      });
      const del = el("button", "link", "删除");
      del.type = "button";
      del.addEventListener("click", () => {
        p.draft.splice(i, 1);
        renderPlan();
      });
      row.append(el("span", "num", pad(i + 1)), input, del);
      box.append(row);
    });
    const add = el("button", "link", "+ 添加一步");
    add.type = "button";
    add.style.marginTop = "12px";
    add.addEventListener("click", () => {
      p.draft.push("");
      renderPlan();
      box.lastElementChild?.querySelector("input")?.focus();
    });
    box.append(add);

    const steps = () => p.draft.map((s) => s.trim()).filter(Boolean);
    if (p.awaiting) {
      actions.append(
        pill("取消", (ev) => {
          ev.target.disabled = true;
          postDecision("cancel");
        }),
        pill(
          "按这个计划开始",
          (ev) => {
            if (!steps().length) return;
            ev.target.disabled = true;
            postDecision("continue", steps());
          },
          true
        )
      );
    } else {
      actions.append(
        pill("取消修改", () => {
          p.editing = false;
          renderPlan();
        }),
        pill(
          "保存修改",
          () => {
            if (!steps().length) return;
            fetch(`/api/runs/${view.id}/plan`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ steps: steps() }),
            });
            p.editing = false;
            p.pendingEdit = true;
            renderPlan();
          },
          true
        )
      );
    }
    return;
  }

  p.steps.forEach((text, i) => {
    const progress = p.state[i] || "pending";
    const row = el("div", "step " + ({ in_progress: "current", done: "done", failed: "failed" }[progress] || ""));
    const body = el("span");
    body.append(el("span", "step-text", text));
    if (p.notes[i]) body.append(el("span", "step-note", (progress === "failed" ? "没做成：" : "") + p.notes[i]));
    row.append(el("span", "num", pad(i + 1)), body, el("span"));
    box.append(row);
  });

  if (p.pendingEdit) actions.append(el("span", "act-detail", "修改会在下一步生效"));
  else if (view.live && view.running && p.steps.length && !p.awaiting && p.source !== "cache") {
    actions.append(
      pill("修改计划", () => {
        p.editing = true;
        p.draft = [...p.steps];
        renderPlan();
      })
    );
  } else if (p.awaiting) {
    actions.append(el("span", "act-detail", "这一步需要在运行时确认"));
  }
}
