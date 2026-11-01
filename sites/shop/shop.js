// 模拟商城的公共数据和函数（所有页面共用）。
// 购物车放在 sessionStorage：同一个浏览器标签页里跨页面保留，每次 Agent 新开浏览器都是空的。

const PRODUCTS = [
  { id: "p1", name: "云跑 Pro 碳板跑步鞋", price: 899, rating: 4.9, sales: 1200, ad: true,
    tags: ["跑步鞋", "运动鞋", "鞋"], colors: ["黑色", "白色"], sizes: [39, 40, 41, 42, 43, 44] },
  { id: "p2", name: "轻风 透气跑步鞋", price: 329, rating: 4.6, sales: 8600,
    tags: ["跑步鞋", "运动鞋", "鞋"], colors: ["黑色", "白色", "蓝色"], sizes: [39, 40, 41, 42, 43, 44] },
  { id: "p3", name: "疾速 入门训练跑步鞋", price: 259, rating: 4.2, sales: 15300,
    tags: ["跑步鞋", "运动鞋", "鞋"], colors: ["黑色", "灰色"], sizes: [39, 40, 41, 42, 43] },
  { id: "p4", name: "透气 网面跑步鞋", price: 299, rating: 4.7, sales: 5400,
    tags: ["跑步鞋", "运动鞋", "鞋"], colors: ["白色", "灰色"], sizes: [38, 39, 40, 41, 42, 43] },
  { id: "p5", name: "缓震 慢跑鞋", price: 309, rating: 4.5, sales: 7100,
    tags: ["跑步鞋", "运动鞋", "鞋"], colors: ["黑色", "红色"], sizes: [40, 41, 42, 43, 44, 45] },
  { id: "p6", name: "越野 抓地跑步鞋", price: 459, rating: 4.8, sales: 2300,
    tags: ["跑步鞋", "运动鞋", "鞋"], colors: ["黑色", "绿色"], sizes: [40, 41, 42, 43, 44] },
  { id: "p7", name: "运动速干袜（3 双装）", price: 39, rating: 4.6, sales: 22000,
    tags: ["袜子", "运动"], colors: ["黑色", "白色"], sizes: [] },
  { id: "p8", name: "运动保温水杯 750ml", price: 89, rating: 4.4, sales: 9800,
    tags: ["水杯", "运动"], colors: ["黑色", "蓝色"], sizes: [] },
];

const SHIPPING = { std: { text: "标准配送（3–5 天）", fee: 0 }, fast: { text: "加急配送（次日达）", fee: 15 } };
const PAYMENT = { cod: "货到付款", balance: "模拟余额支付" };

function findProduct(id) {
  return PRODUCTS.find((p) => p.id === id);
}

function money(n) {
  return "¥" + n.toFixed(2);
}

function loadCart() {
  try {
    return JSON.parse(sessionStorage.getItem("cart") || "[]");
  } catch {
    return [];
  }
}

function saveCart(cart) {
  sessionStorage.setItem("cart", JSON.stringify(cart));
  updateCartBadge();
}

// 同一商品、同一颜色、同一尺码合并成一行
function addToCart(id, color, size, qty) {
  const cart = loadCart();
  const line = cart.find((l) => l.id === id && l.color === color && l.size === size);
  if (line) line.qty += qty;
  else cart.push({ id, color, size, qty });
  saveCart(cart);
}

function cartTotal(cart) {
  return cart.reduce((sum, l) => sum + findProduct(l.id).price * l.qty, 0);
}

function updateCartBadge() {
  const el = document.getElementById("cart-count");
  if (el) el.textContent = loadCart().reduce((n, l) => n + l.qty, 0);
}

// 同样的订单内容总是得到同样的订单号，方便事后核对 Agent 报的对不对
function orderId(s) {
  let h = 5381;
  for (const ch of s) h = ((h * 33) ^ ch.codePointAt(0)) >>> 0;
  return "OD-" + h.toString(16).toUpperCase().padStart(8, "0");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// 每页顶部的搜索栏和购物车入口
function renderHeader(q = "") {
  document.getElementById("top").innerHTML = `
    <a class="logo" href="index.html">小铺</a>
    <form class="search" action="search.html" method="get">
      <input type="text" name="q" value="${escapeHtml(q)}" placeholder="搜索商品，例如 跑步鞋">
      <button type="submit" class="btn">搜索</button>
    </form>
    <a class="cart-link" href="cart.html">购物车（<span id="cart-count">0</span>）</a>
    <p class="tag">本地测试站 · 仅用于 Agent 实验 · 不会真实扣款</p>`;
  updateCartBadge();
}
