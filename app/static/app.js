// Web Agent 前端：发任务、确认和修改计划、实时显示每一步、处理确认和接管、回看历史。

const THINKING_LABEL = { minimal: "极速", low: "快速", medium: "均衡", high: "深入" };

// [显示文字, 状态点样式]
const STATUS = {
  RUNNING: ["运行中", "running"],
  WAITING: ["等你决定", "waiting"],
  PLANNING: ["正在拟定计划", "running"],
  COMPLETE: ["已完成", "good"],
  GAVE_UP: ["没能完成", "bad"],
  INCOMPLETE: ["没能完成", "bad"],
  STUCK: ["卡住了", "bad"],
  TIMEOUT: ["超时了", "bad"],
  MAX_STEPS: ["步数用完了", "bad"],
  SAFETY_STOP: ["已取消", ""],
  CANCELLED: ["已取消", ""],
  STOPPED: ["已停止", ""],
  ERROR: ["出错了", "bad"],
  UNKNOWN: ["未结束", ""],
};

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

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};
const pad = (n) => String(n).padStart(2, "0");

function pill(text, onClick, dark) {
  const b = el("button", "pill" + (dark ? " dark" : ""), text);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

// ---------- 选项（记在本机浏览器里，读写失败也不影响使用） ----------
const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem("webagent." + key);
      return v == null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem("webagent." + key, JSON.stringify(value));
    } catch {}
  },
};

const opts = {
  url: store.get("url", "http://127.0.0.1:8770/index.html"),
  model: store.get("model", "gemini-3.6-flash"),
  thinking: store.get("thinking", "low"),
  auto: store.get("auto", false),
  plan: store.get("plan", true),
  cache: store.get("cache", true),
};

// ---------- 当前查看的这次运行 ----------
let view = null;

function freshView(id, live) {
  return {
    id,
    live,
    running: live,
    source: null,
    shots: [],
    steps: 0,
    selected: -1,
    following: true,
    thought: null,
    startTime: null,
    meta: {},
    status: live ? "RUNNING" : "UNKNOWN",
    cards: [],
    plan: { steps: [], state: [], notes: [], awaiting: false, editing: false, draft: null, pendingEdit: false },
  };
}

// ---------- 页面切换 ----------
function show(page) {
  $("home-view").hidden = page !== "home";
  $("history-view").hidden = page !== "history";
  $("run-view").hidden = page !== "run";
  $("nav-history").classList.toggle("on", page === "history");
  window.scrollTo({ top: 0 });
}

function goHome() {
  if (view && view.source) view.source.close();
  view = null;
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
    if (view && view.live && view.running) stopRun();
    else startRun();
  });

  $("home").addEventListener("click", goHome);
  $("nav-new").addEventListener("click", goHome);
  $("nav-history").addEventListener("click", () => {
    if (view && view.source && !view.running) view.source.close();
    show("history");
    refreshHistory();
  });
  $("follow-live").addEventListener("click", () => {
    view.following = true;
    showShot(view.shots.length - 1);
  });

  updateSendButton();
  show("home");
}

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

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function updateHint() {
  const parts = [hostOf(opts.url), (THINKING_LABEL[opts.thinking] || opts.thinking) + "思考"];
  if (opts.plan) parts.push("先拟计划");
  if (opts.auto) parts.push("自动确认");
  if (!opts.cache) parts.push("不用缓存");
  $("target-hint").textContent = parts.join(" · ");
}

function updateSendButton() {
  const running = view && view.live && view.running;
  $("send").textContent = running ? "停止" : "开始 →";
  $("send").classList.toggle("dark", !running);
  $("query").disabled = Boolean(running);
  $("query").placeholder = running ? "当前任务正在执行" : "描述一个任务，比如：预订明天下午的会议室";
}

// ---------- 历史 ----------
function timeLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? hm : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

function refreshHistory() {
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

// ---------- 发起、停止、决定 ----------
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
  fetch(`/api/runs/${view.id}/stop`, { method: "POST" });
  renderMeta("正在停止…");
}

function postDecision(value, plan) {
  return fetch(`/api/runs/${view.id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value, plan }),
  });
}

// ---------- 打开一次运行（实时或回看） ----------
function openRun(id, live) {
  if (view && view.source) view.source.close();
  view = freshView(id, live);
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

  const source = new EventSource(`/api/runs/${id}/events`);
  view.source = source;
  const mine = view;
  source.onmessage = (m) => {
    if (view === mine) handle(JSON.parse(m.data));
  };
  source.onerror = () => {
    // 连接断了就别让浏览器自动重连（重连会把所有事件再放一遍）
    source.close();
    if (view === mine && mine.running) setStatus("UNKNOWN");
  };
}

function setStatus(key) {
  view.status = key;
  renderMeta();
}

function renderMeta(override) {
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

// ---------- 事件处理 ----------
function handle(e) {
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
      if (e.kind === "plan") { planAwaiting(); setMobileTab("plan"); }
      else { addAwaitCard(e); if (view.live && view.running) showAttention(e.kind, e.action, e.text); }
      break;
    case "decision":
      if (e.kind === "plan") planDecided(e);
      else { resolveCard(e); if (pendingAttention?.kind === e.kind) hideAttention(); }
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

// ---------- 计划 ----------
function setPlan(steps, source) {
  const p = view.plan;
  // 改过的计划：同一位置、同样文字的步骤保留原来的进度
  const state = steps.map((s, i) => (p.steps[i] === s ? p.state[i] : "pending"));
  const notes = steps.map((s, i) => (p.steps[i] === s ? p.notes[i] : ""));
  Object.assign(p, { steps, state, notes, source, pendingEdit: false });
  if (source === "edited") addNote("计划已按你的修改更新");
  $("plan-block").hidden = false;
  renderPlan();
}

function planProgress(e) {
  const p = view.plan;
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

function planAwaiting() {
  const p = view.plan;
  p.awaiting = true;
  if (view.live && view.running) {
    setStatus("WAITING");
    p.editing = true;
    p.draft = [...p.steps];
  }
  renderPlan();
}

function planDecided(e) {
  const p = view.plan;
  p.awaiting = false;
  p.editing = false;
  if (e.value !== "continue") addNote("你取消了这个计划");
  else if (view.running) setStatus("RUNNING");
  renderPlan();
}

function renderPlan() {
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
    const state = p.state[i] || "pending";
    const row = el("div", "step " + ({ in_progress: "current", done: "done", failed: "failed" }[state] || ""));
    const body = el("span");
    body.append(el("span", "step-text", text));
    if (p.notes[i]) body.append(el("span", "step-note", (state === "failed" ? "没做成：" : "") + p.notes[i]));
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

// ---------- 过程 ----------
function describe(name, a) {
  const pos = a.x != null && a.y != null ? `（${a.x}, ${a.y}）` : "";
  const dir = { up: "向上", down: "向下", left: "向左", right: "向右" }[a.direction] || "";
  const typed = `输入“${a.text ?? ""}”${a.press_enter ? "并回车" : ""}`;
  const map = {
    click: "点击" + pos,
    click_at: "点击" + pos,
    double_click: "双击" + pos,
    triple_click: "三击" + pos,
    right_click: "右键" + pos,
    move: "移动鼠标" + pos,
    hover_at: "移动鼠标" + pos,
    type: typed,
    type_text_at: typed,
    scroll: "滚动 " + dir,
    scroll_at: "滚动 " + dir,
    scroll_document: "滚动 " + dir,
    navigate: "打开 " + (a.url || ""),
    search: "打开搜索引擎",
    go_back: "返回上一页",
    go_forward: "前进",
    take_screenshot: "查看当前页面",
    open_web_browser: "查看当前页面",
    wait: "等待页面加载",
    wait_5_seconds: "等待页面加载",
    press_key: "按键 " + (a.key || ""),
    hotkey: "快捷键 " + [].concat(a.keys || []).join(" + "),
    key_combination: "快捷键 " + (a.keys || ""),
    replay: "回放 · " + (a.selector || "按坐标"),
  };
  return map[name] || name;
}

// 缓存配方里的一步，写成给人看的一句话（和后端 describe_step 一致）
function replayTitle(s) {
  const desc = s.desc || "";
  switch (s.op) {
    case "click":
      return desc ? `点击“${desc}”` : "点击";
    case "fill":
      return desc ? `在“${desc}”填写“${s.text}”` : `输入“${s.text}”`;
    case "goto":
      return "打开 " + s.url;
    case "keys":
      return "按键 " + s.keys.join(" + ");
    case "back":
      return "返回上一页";
    default:
      return s.op;
  }
}

function addAction(e) {
  for (const s of $("log").querySelectorAll(".act.current")) s.classList.remove("current");
  const args = e.args || {};
  const detail = describe(e.name, args);
  const row = el("div", "act" + (view.running ? " current" : ""));
  const title = el("span", "act-title", args.intent || detail);
  row.append(title);
  if (args.intent) {
    row.append(el("br"), el("span", "act-detail", detail));
  }

  if (view.thought) {
    const toggle = el("button", "link thought-toggle", "思考");
    toggle.type = "button";
    const thought = el("div", "thought", view.thought);
    thought.hidden = true;
    toggle.addEventListener("click", (ev) => {
      ev.stopPropagation();
      thought.hidden = !thought.hidden;
    });
    row.append(toggle, thought);
    view.thought = null;
  }

  // 动作作用在它执行前的那张截图上：把点击位置记到那张图里
  const shotIndex = view.shots.length - 1;
  if (shotIndex >= 0) {
    row.dataset.shot = shotIndex;
    if (args.x != null && args.y != null) {
      view.shots[shotIndex].ring = { x: args.x, y: args.y };
      if (view.following && view.selected === shotIndex) showShot(shotIndex);
    }
    row.addEventListener("click", () => {
      view.following = false;
      showShot(Number(row.dataset.shot));
    });
  }

  $("log").append(row);
  view.steps += 1;
  if (view.status === "RUNNING") renderMeta();
}

function addNote(text) {
  $("log").append(el("div", "note", "— " + text));
}

function addCard(title, texts) {
  const card = el("div", "card");
  card.append(el("div", "card-title", title));
  for (const t of texts) if (t) card.append(el("div", "card-text", t));
  $("log").append(card);
  return card;
}

function addAwaitCard(e) {
  const isConfirm = e.kind === "confirm";
  const isLogin = e.kind === "login";
  const card = isConfirm
    ? addCard("需要你确认", [`接下来要：${e.action || "执行一个关键操作"}`, e.text])
    : isLogin
      ? addCard("需要你登录", [e.action, e.text])
      : addCard("它好像卡住了", [e.text]);
  card.dataset.kind = e.kind;
  const row = el("div", "pill-row");
  const send = (value) => (ev) => {
    for (const b of row.querySelectorAll("button")) b.disabled = true;
    postDecision(value).then(r => { if (!r.ok) throw new Error("请求失败"); }).catch(() => { for (const b of row.querySelectorAll("button")) b.disabled = false; });
  };
  row.append(
    pill(isConfirm ? "取消" : "结束", send("cancel")),
    pill(isLogin ? "已登录，继续" : "继续", send("continue"), true)
  );
  card.append(row);
  view.cards.push(card);
  if (view.live && view.running) setStatus("WAITING");
  else for (const b of row.querySelectorAll("button")) b.disabled = true;
}

function resolveCard(e) {
  const card = view.cards.findLast((c) => c.dataset.kind === e.kind && !c.classList.contains("resolved"));
  if (!card) return;
  card.classList.add("resolved");
  const go = e.value === "continue";
  const stopped = { confirm: "你取消了这个操作", login: "你选择不登录" }[e.kind] || "你结束了任务";
  card.append(el("div", "card-result", go ? "你选择了继续" : stopped));
  if (view.running) setStatus("RUNNING");
}

// ---------- 画面 ----------
function addShot(e) {
  const index = view.shots.length;
  view.shots.push({ url: e.url, src: `/api/runs/${view.id}/files/${e.file}`, ring: null });
  const thumb = el("button", "thumb");
  thumb.type = "button";
  thumb.setAttribute("aria-label", `第 ${index + 1} 张截图`);
  const img = el("img");
  img.src = view.shots[index].src;
  img.alt = "";
  img.loading = "lazy";
  thumb.append(img);
  thumb.addEventListener("click", () => {
    view.following = false;
    showShot(index);
  });
  $("filmstrip").append(thumb);
  if (view.following) showShot(index);
  else $("counter").textContent = `${pad(view.selected + 1)} / ${pad(view.shots.length)}`;
}

function showShot(index) {
  const shot = view.shots[index];
  if (!shot) return;
  view.selected = index;
  $("screen-empty").hidden = true;
  $("shot").src = shot.src;
  $("url").textContent = shot.url;
  $("counter").textContent = `${pad(index + 1)} / ${pad(view.shots.length)}`;
  const ring = $("ring");
  if (shot.ring) {
    ring.style.left = shot.ring.x / 10 + "%";
    ring.style.top = shot.ring.y / 10 + "%";
    ring.hidden = false;
  } else {
    ring.hidden = true;
  }
  [...$("filmstrip").children].forEach((t, i) => t.classList.toggle("selected", i === index));
  $("filmstrip").children[index]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  for (const row of $("log").querySelectorAll(".act[data-shot]")) {
    row.classList.toggle("selected", !view.following && Number(row.dataset.shot) === index);
  }
  $("follow-live").hidden = view.following || !view.running;
}

// 等你登录或接管时，后端每隔一两秒推一帧浏览器的实时画面（不进胶片条，只占用大图）
function showLive(e) {
  if (!view.running) return;
  view.following = true;
  $("screen-empty").hidden = true;
  $("shot").src = `/api/runs/${view.id}/files/live.jpg?n=${e.n}`;
  $("url").textContent = e.url;
  $("counter").textContent = "实时画面";
  $("ring").hidden = true;
  for (const t of $("filmstrip").children) t.classList.remove("selected");
}

// ---------- 结束 ----------
function finish(e) {
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

// 只支持回答里常见的几种格式：加粗、行内代码、列表、段落
function markdown(text) {
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const out = [];
  let list = null;
  let para = [];
  const flushPara = () => {
    if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    para = [];
  };
  const flushList = () => {
    if (list) out.push(`<ul>${list.map((i) => `<li>${inline(i)}</li>`).join("")}</ul>`);
    list = null;
  };
  for (const line of text.split("\n")) {
    const item = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
    if (item) {
      flushPara();
      (list ||= []).push(item[1]);
    } else if (!line.trim()) {
      flushPara();
      flushList();
    } else {
      flushList();
      para.push(line.trim());
    }
  }
  flushPara();
  flushList();
  return out.join("");
}


/* Console v2 controls: responsive task views, screenshot zoom, and reliable confirmations. */
let mobileTab = "screen";
let shotZoom = 1;
let shotPan = { x: 0, y: 0 };
let draggingShot = null;
let pendingAttention = null;
function setMobileTab(tab) {
  mobileTab = tab;
  $("run-view").dataset.mobileTab = tab;
  for (const b of document.querySelectorAll("[data-mobile-tab]")) b.setAttribute("aria-pressed", String(b.dataset.mobileTab === tab));
}
function resetShotZoom() { shotZoom = 1; shotPan = { x: 0, y: 0 }; renderShotZoom(); }
function renderShotZoom() {
  const frame = $("screen");
  const maxX = (frame.clientWidth * (shotZoom - 1)) / 2;
  const maxY = (frame.clientHeight * (shotZoom - 1)) / 2;
  shotPan.x = Math.max(-maxX, Math.min(maxX, shotPan.x));
  shotPan.y = Math.max(-maxY, Math.min(maxY, shotPan.y));
  $("shot-transform").style.transform = `translate(${shotPan.x}px, ${shotPan.y}px) scale(${shotZoom})`;
  frame.classList.toggle("zoomed", shotZoom > 1);
  $("screen-zoom-reset").textContent = Math.round(shotZoom * 100) + "%";
  $("screen-zoom-out").disabled = shotZoom <= 1;
  $("screen-zoom-in").disabled = shotZoom >= 3;
}
function changeShotZoom(amount) {
  shotZoom = Math.max(1, Math.min(3, Math.round((shotZoom + amount) * 100) / 100));
  renderShotZoom();
}
function showAttention(kind, action, explanation) {
  pendingAttention = { kind, runId: view?.id };
  $("attention-title").textContent = kind === "login" ? "需要你登录" : kind === "confirm" ? "需要确认关键操作" : "Agent 需要帮助";
  $("attention-text").textContent = [action, explanation].filter(Boolean).join(" · ");
  $("attention-panel").hidden = false;
  $("attention-error").hidden = true;
  $("attention-continue").textContent = kind === "login" ? "已登录，继续" : kind === "confirm" ? "确认并继续" : "继续执行";
  setMobileTab("plan");
}
function hideAttention() { pendingAttention = null; $("attention-panel").hidden = true; }
async function submitAttention(value) {
  if (!pendingAttention || !view || pendingAttention.runId !== view.id) return;
  const buttons = [$("attention-cancel"), $("attention-continue")];
  buttons.forEach(b => b.disabled = true);
  try {
    const response = await postDecision(value);
    if (!response.ok) throw new Error((await response.json().catch(()=>({}))).detail || "请求失败");
    hideAttention();
  } catch (error) {
    $("attention-error").textContent = "操作未提交：" + error.message + "。请重试。";
    $("attention-error").hidden = false;
  } finally {
    buttons.forEach(b => b.disabled = false);
  }
}
for (const b of document.querySelectorAll("[data-mobile-tab]")) b.addEventListener("click", () => setMobileTab(b.dataset.mobileTab));
$("screen-zoom-in").addEventListener("click", () => changeShotZoom(.25));
$("screen-zoom-out").addEventListener("click", () => changeShotZoom(-.25));
$("screen-zoom-reset").addEventListener("click", resetShotZoom);
const screenFrame = $("screen");
screenFrame.addEventListener("pointerdown", (event) => {
  if (shotZoom <= 1 || !event.isPrimary) return;
  draggingShot = { id:event.pointerId, x:event.clientX, y:event.clientY };
  screenFrame.setPointerCapture(event.pointerId);
});
screenFrame.addEventListener("pointermove", (event) => {
  if (!draggingShot || draggingShot.id !== event.pointerId) return;
  shotPan.x += event.clientX - draggingShot.x;
  shotPan.y += event.clientY - draggingShot.y;
  draggingShot.x = event.clientX;
  draggingShot.y = event.clientY;
  renderShotZoom();
});
function endShotDrag(event) {
  if (draggingShot?.id === event.pointerId) draggingShot = null;
}
screenFrame.addEventListener("pointerup", endShotDrag);
screenFrame.addEventListener("pointercancel", endShotDrag);

$("attention-cancel").addEventListener("click", () => submitAttention("cancel"));
$("attention-continue").addEventListener("click", () => submitAttention("continue"));

init();
