// 左边的“过程”：每一步动作、说明文字、需要你决定的卡片。

import { $, el, pill } from "./util.js";
import { state } from "./state.js";
import { postDecision, renderMeta, setStatus } from "./ui.js";
import { showShot } from "./screen.js";

// 模型的一个动作，写成给人看的一句话
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
    list_tabs: "查看打开的标签页",
    switch_tab: `切换到第 ${a.index ?? "?"} 个标签页`,
    close_tab: `关闭第 ${a.index ?? "?"} 个标签页`,
  };
  return map[name] || name;
}

// 缓存配方里的一步，写成给人看的一句话（和后端 describe_step 一致）
export function replayTitle(s) {
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

export function addAction(e) {
  const view = state.view;
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
      state.view.following = false;
      showShot(Number(row.dataset.shot));
    });
  }

  $("log").append(row);
  view.steps += 1;
  if (view.status === "RUNNING") renderMeta();
}

export function addNote(text) {
  $("log").append(el("div", "note", "— " + text));
}

export function addCard(title, texts) {
  const card = el("div", "card");
  card.append(el("div", "card-title", title));
  for (const t of texts) if (t) card.append(el("div", "card-text", t));
  $("log").append(card);
  return card;
}

export function addAwaitCard(e) {
  const view = state.view;
  const isConfirm = e.kind === "confirm";
  const isLogin = e.kind === "login";
  const isPause = e.kind === "pause";
  const card = isConfirm
    ? addCard("需要你确认", [`接下来要：${e.action || "执行一个关键操作"}`, e.text])
    : isLogin
      ? addCard("需要你登录", [e.action, e.text])
      : isPause
        ? addCard(e.action ? "出错了，已暂停" : "已暂停", [e.action, e.text])
        : addCard("它好像卡住了", [e.text]);
  card.dataset.kind = e.kind;
  const row = el("div", "pill-row");
  const setDisabled = (disabled) => {
    for (const b of row.querySelectorAll("button")) b.disabled = disabled;
  };
  const send = (value) => () => {
    setDisabled(true);
    postDecision(value)
      .then((r) => {
        if (!r.ok) throw new Error("请求失败");
      })
      .catch(() => setDisabled(false));
  };
  row.append(
    pill(isConfirm ? "取消" : "结束", send("cancel")),
    pill(isLogin ? "已登录，继续" : "继续", send("continue"), true)
  );
  card.append(row);
  view.cards.push(card);
  if (view.live && view.running) setStatus("WAITING");
  else setDisabled(true);
}

export function resolveCard(e) {
  const view = state.view;
  const card = view.cards.findLast((c) => c.dataset.kind === e.kind && !c.classList.contains("resolved"));
  if (!card) return;
  card.classList.add("resolved");
  const go = e.value === "continue";
  const stopped = { confirm: "你取消了这个操作", login: "你选择不登录" }[e.kind] || "你结束了任务";
  card.append(el("div", "card-result", go ? "你选择了继续" : stopped));
  if (view.running) setStatus("RUNNING");
}
