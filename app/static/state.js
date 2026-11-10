// 共享状态：所有模块都从这里读写同一份，不各自保存副本。
// 模块依赖只往一个方向走，避免循环：
//   util → state → ui → screen → log → plan → events → history → app（入口）

import { store } from "./util.js";

export const THINKING_LABEL = { minimal: "极速", low: "快速", medium: "均衡", high: "深入" };

// [显示文字, 状态点样式]
export const STATUS = {
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

// 这几种等待时可以接管（和后端 app/takeover.py 一致）
export const TAKEOVER_KINDS = ["login", "stuck", "pause"];

// 发任务时的选项
export const opts = {
  url: store.get("url", "http://127.0.0.1:8770/index.html"),
  model: store.get("model", "gemini-3.6-flash"),
  thinking: store.get("thinking", "low"),
  auto: store.get("auto", false),
  plan: store.get("plan", true),
  cache: store.get("cache", true),
};

export const state = {
  view: null,  // 当前查看的这次运行（freshView 的结果）；在首页、历史页时为 null
  attention: null,  // 底部“需要你处理”卡片正在等的事：{ kind, runId }；没有时为 null
};

export function freshView(id, live) {
  return {
    id,
    live,
    running: live,
    needsYou: false,  // 正在等你处理（确认、登录、卡住、暂停）
    pausing: false,  // 点了暂停，等当前这一步做完
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

// 现在能不能接管：正在等你登录、处理卡住或暂停，而且看的就是这次正在跑的运行
export function takeoverActive() {
  const { view, attention } = state;
  return !!(attention && TAKEOVER_KINDS.includes(attention.kind) && view?.live && view.running && attention.runId === view.id);
}
