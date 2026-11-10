// 各模块都要用的界面小件：页面切换、标题下的状态栏、底部的发送/暂停按钮、提交决定。

import { $, el } from "./util.js";
import { STATUS, state, takeoverActive } from "./state.js";

// ---------- 页面切换 ----------
export function show(page) {
  $("home-view").hidden = page !== "home";
  $("history-view").hidden = page !== "history";
  $("run-view").hidden = page !== "run";
  $("nav-history").classList.toggle("on", page === "history");
  window.scrollTo({ top: 0 });
}

// ---------- 状态栏 ----------
export function setStatus(key) {
  state.view.status = key;
  renderMeta();
}

export function renderMeta(override) {
  const view = state.view;
  const meta = $("meta");
  meta.replaceChildren();
  const [label, cls] = STATUS[view.status] || STATUS.UNKNOWN;
  let text = override || label;
  if (!override && view.status === "RUNNING" && view.steps) text += ` · 第 ${view.steps} 步`;
  meta.append(el("span", "state " + cls, text));
  for (const item of [view.meta.host, view.meta.model, view.meta.thinking]) {
    if (item) meta.append(el("span", null, item));
  }
}

// ---------- 发送 / 停止 / 暂停按钮 ----------
export function updateSendButton() {
  const view = state.view;
  const running = view && view.live && view.running;
  $("send").textContent = running ? "停止" : "开始 →";
  $("send").classList.toggle("dark", !running);
  $("query").disabled = Boolean(running);
  $("query").placeholder = running ? "当前任务正在执行" : "描述一个任务，比如：预订明天下午的会议室";
  // 已经停下来等你的时候不需要暂停
  $("pause").hidden = !running || view.needsYou || view.plan.awaiting;
  $("pause").disabled = Boolean(view?.pausing);
  $("pause").textContent = view?.pausing ? "正在暂停…" : "暂停";
  // Agent 执行时点画面就能接管：给画面一个“能点”的光标（缓存回放不行）
  const canTakeOver = running && !view.needsYou && !view.pausing && !view.plan.awaiting && view.plan.source !== "cache";
  $("run-view").dataset.canTakeOver = String(Boolean(canTakeOver));
}

// ---------- 把你的决定（继续 / 取消）发给后端 ----------
export function postDecision(value, plan) {
  // 接管时写给模型的话，跟着决定一起发出去
  const note = takeoverActive() ? $("takeover-note").value.trim() : "";
  return fetch(`/api/runs/${state.view.id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value, plan, note: note || null }),
  });
}
