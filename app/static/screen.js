// 右边的浏览器画面：截图和胶片条、实时画面、放大拖动、手机上的视图切换，
// 以及“需要你处理”卡片和接管（直接在画面上点击、滚动、输入）。

import { $, el, pad } from "./util.js";
import { TAKEOVER_KINDS, state, takeoverActive } from "./state.js";
import { postDecision, updateSendButton } from "./ui.js";

// 只有画面模块自己用的状态
let shotZoom = 1;
let shotPan = { x: 0, y: 0 };
let draggingShot = null;  // 正在拖动放大后的画面
let pointerStart = null;  // 按下时的位置，用来区分“点击”和“拖动”
let scrollBuffer = null;  // 攒着还没发出去的滚动

// ---------- 截图 ----------
export function addShot(e) {
  const view = state.view;
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
    state.view.following = false;
    showShot(index);
  });
  $("filmstrip").append(thumb);
  if (view.following) showShot(index);
  else $("counter").textContent = `${pad(view.selected + 1)} / ${pad(view.shots.length)}`;
}

export function showShot(index) {
  const view = state.view;
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
export function showLive(e) {
  const view = state.view;
  if (!view.running) return;
  view.following = true;
  $("screen-empty").hidden = true;
  $("shot").src = `/api/runs/${view.id}/files/live.jpg?n=${e.n}`;
  // 登录页上的二维码单独截了一张清晰的，在登录卡片里放大显示（整页画面缩小后扫不出来）
  if (state.attention?.kind === "login") {
    $("attention-qr").hidden = !e.qr;
    if (e.qr) $("attention-qr").src = `/api/runs/${view.id}/files/qr.png?n=${e.n}`;
  }
  $("url").textContent = e.url;
  $("counter").textContent = "实时画面";
  $("ring").hidden = true;
  for (const t of $("filmstrip").children) t.classList.remove("selected");
}

// ---------- 手机上的视图切换、截图放大 ----------
export function setMobileTab(tab) {
  $("run-view").dataset.mobileTab = tab;
  for (const b of document.querySelectorAll("[data-mobile-tab]")) b.setAttribute("aria-pressed", String(b.dataset.mobileTab === tab));
}

export function resetShotZoom() {
  shotZoom = 1;
  shotPan = { x: 0, y: 0 };
  renderShotZoom();
}

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

// ---------- 接管 ----------
// 屏幕上的一点 → 截图里的位置（0–1 的比例）。图片按比例缩放、四周可能留白，还可能被放大拖动过，
// 所以用图片当前在屏幕上的实际位置来算；点在留白处返回 null
function pointOnShot(clientX, clientY) {
  const img = $("shot");
  if (!img.naturalWidth) return null;
  const r = img.getBoundingClientRect();
  const scale = Math.min(r.width / img.naturalWidth, r.height / img.naturalHeight);
  const w = img.naturalWidth * scale, h = img.naturalHeight * scale;
  const x = (clientX - (r.left + (r.width - w) / 2)) / w;
  const y = (clientY - (r.top + (r.height - h) / 2)) / h;
  return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
}

function showTakeoverRing(p) {
  const ring = $("ring");
  ring.style.left = p.x * 100 + "%";
  ring.style.top = p.y * 100 + "%";
  ring.hidden = false;
  setTimeout(() => { ring.hidden = true; }, 800);
}

async function sendTakeover(input) {
  try {
    const response = await fetch(`/api/runs/${state.view.id}/input`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "请求失败");
    $("attention-error").hidden = true;
    return true;
  } catch (error) {
    $("attention-error").textContent = "操作没发出去：" + error.message;
    $("attention-error").hidden = false;
    return false;
  }
}

// ---------- “需要你处理”卡片 ----------
export function showAttention(kind, action, explanation) {
  const view = state.view;
  state.attention = { kind, runId: view?.id };
  view.needsYou = true;
  view.pausing = false;
  updateSendButton();
  $("attention-title").textContent = kind === "login" ? "需要你登录" : kind === "confirm" ? "需要确认关键操作" : kind === "pause" ? (action ? "出错了，已暂停" : "已暂停，由你接管") : "Agent 需要帮助";
  $("attention-text").textContent = [action, explanation].filter(Boolean).join(" · ");
  $("attention-panel").hidden = false;
  $("attention-error").hidden = true;
  $("attention-continue").textContent = kind === "login" ? "已登录，继续" : kind === "confirm" ? "确认并继续" : "继续执行";
  $("attention-cancel").textContent = kind === "confirm" ? "取消操作" : "结束任务";
  $("takeover-note").hidden = kind === "login";
  // 等你登录、处理卡住时可以接管：直接在画面上点、滚动、输入
  const takeover = TAKEOVER_KINDS.includes(kind);
  $("takeover").hidden = !takeover;
  $("attention-qr").hidden = true;
  $("screen").classList.toggle("takeover-on", takeover);
  $("attention-panel").classList.toggle("docked", takeover);  // 接管时卡片靠左，不挡住右边的画面
  setMobileTab(takeover ? "screen" : "plan");
}

export function hideAttention() {
  state.attention = null;
  if (state.view) state.view.needsYou = false;
  updateSendButton();
  $("takeover-note").value = "";
  $("attention-panel").hidden = true;
  $("takeover").hidden = true;
  $("screen").classList.remove("takeover-on");
}

async function submitAttention(value) {
  const { view, attention } = state;
  if (!attention || !view || attention.runId !== view.id) return;
  const buttons = [$("attention-cancel"), $("attention-continue")];
  buttons.forEach((b) => (b.disabled = true));
  try {
    const response = await postDecision(value);
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "请求失败");
    hideAttention();
  } catch (error) {
    $("attention-error").textContent = "操作未提交：" + error.message + "。请重试。";
    $("attention-error").hidden = false;
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

// ---------- 绑定事件：整个页面只调用一次（由 app.js 的 init 调用） ----------
export function initScreen() {
  for (const b of document.querySelectorAll("[data-mobile-tab]")) b.addEventListener("click", () => setMobileTab(b.dataset.mobileTab));
  $("screen-zoom-in").addEventListener("click", () => changeShotZoom(0.25));
  $("screen-zoom-out").addEventListener("click", () => changeShotZoom(-0.25));
  $("screen-zoom-reset").addEventListener("click", resetShotZoom);
  $("follow-live").addEventListener("click", () => {
    state.view.following = true;
    showShot(state.view.shots.length - 1);
  });

  const screenFrame = $("screen");
  screenFrame.addEventListener("pointerdown", (event) => {
    if (event.isPrimary) pointerStart = { x: event.clientX, y: event.clientY };
    if (shotZoom <= 1 || !event.isPrimary) return;
    draggingShot = { id: event.pointerId, x: event.clientX, y: event.clientY };
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
  const endShotDrag = (event) => {
    if (draggingShot?.id === event.pointerId) draggingShot = null;
  };
  screenFrame.addEventListener("pointerup", (event) => {
    endShotDrag(event);
    // 接管时：几乎没移动就算点击（移动多了是在拖动放大后的画面）
    if (takeoverActive() && pointerStart && Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) < 6) {
      const p = pointOnShot(event.clientX, event.clientY);
      if (p) {
        sendTakeover({ type: "click", x: p.x, y: p.y });
        showTakeoverRing(p);
      }
    }
    pointerStart = null;
  });
  screenFrame.addEventListener("pointercancel", (event) => {
    endShotDrag(event);
    pointerStart = null;
  });

  // 接管时滚轮滚动网页：攒 250 毫秒再发一次，避免一下发几十个请求
  screenFrame.addEventListener("wheel", (event) => {
    if (!takeoverActive()) return;
    const p = pointOnShot(event.clientX, event.clientY);
    if (!p) return;
    event.preventDefault();
    if (scrollBuffer) { scrollBuffer.dy += event.deltaY; return; }
    scrollBuffer = { x: p.x, y: p.y, dy: event.deltaY };
    setTimeout(() => {
      const b = scrollBuffer;
      scrollBuffer = null;
      sendTakeover({ type: "scroll", x: b.x, y: b.y, dy: Math.max(-3000, Math.min(3000, b.dy)) });
    }, 250);
  }, { passive: false });

  $("takeover-send").addEventListener("click", () => {
    const text = $("takeover-text").value;
    if (!text) return;
    sendTakeover({ type: "type", text }).then((ok) => { if (ok) $("takeover-text").value = ""; });
  });
  $("takeover-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); $("takeover-send").click(); }
  });
  for (const b of document.querySelectorAll("[data-takeover-key]")) {
    b.addEventListener("click", () => sendTakeover({ type: "key", key: b.dataset.takeoverKey }));
  }

  $("attention-cancel").addEventListener("click", () => submitAttention("cancel"));
  $("attention-continue").addEventListener("click", () => submitAttention("continue"));
}
