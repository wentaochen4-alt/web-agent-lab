// Web Agent 前端入口：首页、选项、发起 / 停止 / 暂停任务，并把各模块接起来。
//   util.js     工具函数
//   state.js    共享状态（当前查看的运行、选项、等你处理的事）
//   ui.js       页面切换、状态栏、发送按钮、提交决定
//   screen.js   浏览器画面、实时画面、接管、“需要你处理”卡片
//   log.js      过程
//   plan.js     计划
//   events.js   打开一次运行、接收并分发事件
//   history.js  历史记录
// 所有按钮的事件都只在 init 里绑定一次。

import { $, el, hostOf, pad, store } from "./util.js";
import { THINKING_LABEL, opts, state } from "./state.js";
import { renderMeta, show, updateSendButton } from "./ui.js";
import { initScreen } from "./screen.js";
import { openRun } from "./events.js";
import { refreshHistory } from "./history.js";

// 首页的示例任务。只是示例：Agent 不限于这些网站和任务类型
const EXAMPLES = [
  {
    title: "预订会议室",
    sub: "本地测试站 · 填表并提交",
    query: "在这个会议室预订网站上，预订 A 会议室：日期 2026-11-05，下午时段，预订人填写“测试用户”。提交后告诉我预约编号。",
    url: "http://127.0.0.1:8770/index.html",
  },
  {
    title: "查维基百科",
    sub: "wikipedia.org · 只浏览",
    query: "在维基百科上找到论文 Attention Is All You Need 的词条，告诉我它发表于哪一年、一共有几位作者。只浏览，不要登录或编辑。",
    url: "https://en.wikipedia.org/wiki/Main_Page",
  },
  {
    title: "看 Hacker News 热帖",
    sub: "news.ycombinator.com · 只浏览",
    query: "告诉我 Hacker News 首页排名第 1 的帖子标题，再打开它的评论页，告诉我一共有多少条评论。只浏览，不要登录、投票或评论。",
    url: "https://news.ycombinator.com/",
  },
];

function goHome() {
  if (state.view && state.view.source) state.view.source.close();
  state.view = null;
  show("home");
  updateSendButton();
  $("query").focus();
}

// ---------- 初始化 ----------
function init() {
  const examples = $("examples");
  EXAMPLES.forEach((ex, i) => {
    const row = el("button", "row");
    row.type = "button";
    const body = el("span");
    body.append(el("span", "row-title", ex.title), el("span", "row-sub", ex.sub));
    row.append(el("span", "num", pad(i + 1)), body, el("span", "row-side", "填入 →"));
    row.addEventListener("click", () => {
      $("query").value = ex.query;
      opts.url = ex.url;
      syncOptions();
      autoGrow();
      $("query").focus();
    });
    examples.append(row);
  });

  fetch("/api/config")
    .then((r) => r.json())
    .then((cfg) => {
      for (const m of cfg.models) $("opt-model").append(new Option(m, m));
      for (const t of cfg.thinking) {
        const b = el("button", null, THINKING_LABEL[t] || t);
        b.type = "button";
        b.setAttribute("role", "radio");
        b.dataset.value = t;
        b.addEventListener("click", () => {
          opts.thinking = t;
          syncOptions();
        });
        $("opt-thinking").append(b);
      }
      syncOptions();
    });

  $("opt-url").addEventListener("input", (e) => ((opts.url = e.target.value), saveOptions()));
  $("opt-model").addEventListener("change", (e) => ((opts.model = e.target.value), saveOptions()));
  $("opt-auto").addEventListener("change", (e) => ((opts.auto = e.target.checked), saveOptions()));
  $("opt-plan").addEventListener("change", (e) => ((opts.plan = e.target.checked), saveOptions()));
  $("opt-cache").addEventListener("change", (e) => ((opts.cache = e.target.checked), saveOptions()));

  $("toggle-options").addEventListener("click", () => {
    $("options").hidden = !$("options").hidden;
  });

  const q = $("query");
  q.addEventListener("input", autoGrow);
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const view = state.view;
    if (view && view.live && view.running) stopRun();
    else startRun();
  });
  $("pause").addEventListener("click", pauseRun);

  $("home").addEventListener("click", goHome);
  $("nav-new").addEventListener("click", goHome);
  $("nav-history").addEventListener("click", () => {
    const view = state.view;
    if (view && view.source && !view.running) view.source.close();
    show("history");
    refreshHistory();
  });

  initScreen();
  updateSendButton();
  show("home");
}

// ---------- 选项 ----------
function autoGrow() {
  const q = $("query");
  q.style.height = "auto";
  q.style.height = Math.min(q.scrollHeight, 140) + "px";
}

function saveOptions() {
  for (const k of Object.keys(opts)) store.set(k, opts[k]);
  updateHint();
}

function syncOptions() {
  $("opt-url").value = opts.url;
  if ($("opt-model").options.length) $("opt-model").value = opts.model;
  $("opt-auto").checked = opts.auto;
  $("opt-plan").checked = opts.plan;
  $("opt-cache").checked = opts.cache;
  for (const b of $("opt-thinking").children) {
    b.setAttribute("aria-checked", String(b.dataset.value === opts.thinking));
  }
  saveOptions();
}

function updateHint() {
  const parts = [hostOf(opts.url), (THINKING_LABEL[opts.thinking] || opts.thinking) + "思考"];
  if (opts.plan) parts.push("先拟计划");
  if (opts.auto) parts.push("自动确认");
  if (!opts.cache) parts.push("不用缓存");
  $("target-hint").textContent = parts.join(" · ");
}

// ---------- 发起、停止、暂停 ----------
function startRun() {
  const query = $("query").value.trim();
  if (!query) {
    $("query").focus();
    return;
  }
  const body = {
    query,
    start_url: opts.url.trim(),
    model: opts.model,
    thinking: opts.thinking,
    auto_confirm: opts.auto,
    plan: opts.plan,
    use_cache: opts.cache,
  };
  fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(async (r) => {
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "启动失败");
      $("query").value = "";
      autoGrow();
      $("options").hidden = true;
      openRun(data.id, true);
    })
    .catch((err) => {
      $("target-hint").textContent = err.message;
    });
}

function stopRun() {
  fetch(`/api/runs/${state.view.id}/stop`, { method: "POST" });
  renderMeta("正在停止…");
}

function pauseRun() {
  const view = state.view;
  view.pausing = true;
  updateSendButton();
  fetch(`/api/runs/${view.id}/pause`, { method: "POST" })
    .then(async (r) => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "请求失败");
      renderMeta("正在暂停，等它想完这一步…");
    })
    .catch((err) => {
      view.pausing = false;
      updateSendButton();
      renderMeta(err.message);
    });
}

init();
