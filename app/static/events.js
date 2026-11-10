// 事件：打开一次运行（实时或回看），接收后端推来的事件，分发给计划、过程、画面各模块。
// 事件类型和后端写进 trajectory.jsonl 的一致，见 CLAUDE.md。

import { $, el, hostOf, markdown } from "./util.js";
import { THINKING_LABEL, freshView, state } from "./state.js";
import { renderMeta, setStatus, show, updateSendButton } from "./ui.js";
import { addShot, hideAttention, resetShotZoom, setMobileTab, showAttention, showLive } from "./screen.js";
import { addAction, addAwaitCard, addCard, addNote, replayTitle, resolveCard } from "./log.js";
import { planAwaiting, planDecided, planProgress, renderPlan, setPlan } from "./plan.js";

// ---------- 打开一次运行（实时或回看） ----------
export function openRun(id, live) {
  if (state.view && state.view.source) state.view.source.close();
  const view = (state.view = freshView(id, live));
  $("title").textContent = "";
  $("log").replaceChildren();
  $("plan").replaceChildren();
  $("plan-actions").replaceChildren();
  $("plan-block").hidden = true;
  $("result").replaceChildren();
  $("result-block").hidden = true;
  $("shot").removeAttribute("src");
  $("ring").hidden = true;
  $("screen-empty").hidden = false;
  $("filmstrip").replaceChildren();
  $("follow-live").hidden = true;
  $("url").textContent = "";
  $("counter").textContent = "";
  show("run");
  hideAttention();
  resetShotZoom();
  setMobileTab("screen");
  renderMeta();
  updateSendButton();

  // 每次打开都新建一条连接；切到别的运行后，旧连接发来的事件一律丢掉
  const source = new EventSource(`/api/runs/${id}/events`);
  view.source = source;
  source.onmessage = (m) => {
    if (state.view === view) handle(JSON.parse(m.data));
  };
  source.onerror = () => {
    // 连接断了就别让浏览器自动重连（重连会把所有事件再放一遍）
    source.close();
    if (state.view === view && view.running) setStatus("UNKNOWN");
  };
}

// ---------- 事件处理 ----------
function handle(e) {
  const view = state.view;
  switch (e.event) {
    case "start":
      view.startTime = e.time;
      $("title").textContent = e.query;
      view.meta = {
        host: e.initial_url ? hostOf(e.initial_url) : "",
        model: e.model,
        thinking: e.thinking ? (THINKING_LABEL[e.thinking] || e.thinking) + "思考" : "",
      };
      renderMeta();
      break;
    case "planning":
      if (view.running) setStatus("PLANNING");
      $("plan-block").hidden = false;
      $("plan").replaceChildren(el("div", "planning", "正在拟定计划…"));
      break;
    case "plan":
      setPlan(e.steps, e.source);
      break;
    case "plan_progress":
      planProgress(e);
      break;
    case "thought":
      view.thought = e.text;
      break;
    case "action":
      addAction(e);
      break;
    case "screenshot":
      addShot(e);
      break;
    case "blocked":
      addNote(`已拦截 ${hostOf(e.url)}`);
      break;
    case "tab_opened":
      addNote(`打开了新标签页（共 ${e.tabs} 个），已切换过去`);
      break;
    case "tab_closed":
      addNote(`关掉了一个标签页（还剩 ${e.tabs} 个）`);
      break;
    case "stuck_nudge":
      addNote("页面一直没有变化，提醒它换个办法");
      break;
    case "human_takeover_done":
      addNote("你处理完了，它从当前页面继续");
      break;
    case "live":
      showLive(e);
      break;
    case "login_done":
      addNote("登录好了，登录状态已保存，它从当前页面继续");
      break;
    case "login_declined":
      addNote("你没有登录，它会说明情况后结束");
      break;
    case "safety_auto_confirm":
      addNote("按你的设置，自动确认了一个关键操作");
      break;
    case "safety_stop":
      addNote("遇到需要确认的操作，已停下");
      break;
    case "awaiting":
      if (e.kind === "plan") {
        planAwaiting();
        setMobileTab("plan");
      } else {
        addAwaitCard(e);
        if (view.live && view.running) showAttention(e.kind, e.action, e.text);
      }
      break;
    case "decision":
      if (e.kind === "plan") planDecided(e);
      else {
        resolveCard(e);
        if (state.attention?.kind === e.kind) hideAttention();
      }
      break;
    case "cache_hit":
      addNote(`这个任务做成功过，照着上次的 ${e.steps} 步直接回放，不调用模型`);
      break;
    case "replay_step":
      addAction({ name: "replay", args: { intent: replayTitle(e.step), selector: e.step.selector } });
      break;
    case "cache_miss":
      addNote(`上次的做法用不上了（${e.reason}），改由 Agent 重新完成`);
      break;
    case "cache_saved":
      addNote(`已记住这次的做法（${e.steps} 步），下次同样的任务直接回放`);
      break;
    case "cache_skipped":
      addNote(`这次的做法没有记住：${e.reason}`);
      break;
    case "error":
      addCard("出了点问题", [e.message]);
      break;
    case "end":
      finish(e);
      break;
    case "__eof":
      view.source.close();
      if (view.running) setStatus("UNKNOWN");
      break;
  }
}

// ---------- 结束 ----------
function finish(e) {
  const view = state.view;
  hideAttention();
  view.running = false;
  for (const s of $("log").querySelectorAll(".act.current")) s.classList.remove("current");
  view.plan.awaiting = false;
  view.plan.editing = false;
  view.plan.state = view.plan.state.map((s) => (s === "in_progress" && e.status === "COMPLETE" ? "done" : s));
  if (view.plan.steps.length) renderPlan();
  setStatus(e.status);

  const box = $("result");
  if (e.final_answer) {
    const answer = el("div", "answer");
    answer.innerHTML = markdown(e.final_answer);
    box.append(answer);
  } else if (e.status !== "CANCELLED") {
    box.append(el("div", "card-text", "它没有给出回答。可以点“过程”里的步骤，看看它在哪一步停住了。"));
  }
  const secs = view.startTime && e.time ? Math.round(e.time - view.startTime) : null;
  const stats = el("div", "result-stats");
  const parts = [`${view.steps} 步`];
  const modeLabel = { REPLAY: "缓存回放", HEALED: "缓存失效后重做" }[e.mode];
  if (modeLabel) parts.push(modeLabel);
  if (secs != null) parts.push(secs >= 60 ? `${Math.floor(secs / 60)} 分 ${secs % 60} 秒` : `${secs} 秒`);
  stats.textContent = parts.join(" · ") + "　";
  if (e.final_answer) {
    const copy = el("button", "link", "拷贝回答");
    copy.type = "button";
    copy.addEventListener("click", () => {
      navigator.clipboard?.writeText(e.final_answer).then(() => (copy.textContent = "已拷贝"));
    });
    stats.append(copy);
  }
  box.append(stats);
  $("result-block").hidden = false;
  $("follow-live").hidden = true;
  updateSendButton();
}
