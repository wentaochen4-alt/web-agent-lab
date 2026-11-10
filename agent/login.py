"""登录相关：登录页识别、二维码查找、二维码过期刷新、登录状态判断。

按网站扩展：每个网站一个 LoginSite 子类，只写和通用做法不一样的地方，再加进 SITES。
没有专门写过的网站用通用做法（LoginSite 本身）。目前只有京东。

这里只帮用户“看清二维码、等用户自己登录”，不输入账号密码，不处理验证码。
"""
from urllib.parse import urlparse

# 网址里出现这些词，基本可以认定是登录页
LOGIN_URL_HINTS = ("login", "signin", "sign-in", "sign_in", "passport")

QR_REFRESH_GAP = 10  # 两次自动刷新之间至少隔几秒
QR_PAUSE_AFTER_INPUT = 30  # 你接管操作后这么多秒内不自动刷新，免得冲掉你正在填的内容

# 在页面上找“像二维码”的图：img / canvas / svg，接近正方形，边长 80–480 像素，完整显示在窗口里，取得分最高的那个。
# canvas，或者 src / class / id 里带 qr、code 的，加倍计分（头像、商品图也可能是正方形，尽量别选错）。
# 返回它在截图里的位置 [x, y, 宽, 高]，找不到返回 null。
FIND_QR_JS = """() => {
  let best = null, bestScore = 0;
  for (const el of document.querySelectorAll("img, canvas, svg")) {
    const r = el.getBoundingClientRect();
    if (r.width < 80 || r.width > 480 || r.left < 0 || r.top < 0 || r.right > innerWidth || r.bottom > innerHeight) continue;
    const ratio = r.width / r.height;
    if (ratio < 0.9 || ratio > 1.1) continue;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || Number(st.opacity) < 0.1) continue;
    const hint = el.tagName === "CANVAS" || /qr|code/i.test((el.getAttribute("src") || "") + " " + el.className + " " + el.id);
    const score = r.width * r.height * (hint ? 2 : 1);
    if (score > bestScore) { bestScore = score; best = [r.x, r.y, r.width, r.height]; }
  }
  return best;
}"""

# 二维码过期了：页面上真的能看到“二维码已失效 / 二维码已过期”之类的字，就点旁边的“刷新”。
# 只认和“二维码”连在一起的说法，“验证码已过期”“优惠券即将过期”不算。
# 返回 null：没过期；true：点了刷新；false：过期了但找不到“刷新”
REFRESH_QR_JS = """() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.bottom <= 0 || r.top >= innerHeight) return false;
    for (let e = el; e; e = e.parentElement) {
      const st = getComputedStyle(e);
      if (st.visibility === "hidden" || st.display === "none" || Number(st.opacity) < 0.1) return false;
    }
    return true;
  };
  const leaves = [...document.querySelectorAll("body *")].filter((el) => el.children.length === 0 && el.textContent.trim().length < 40);
  const expired = /二维码.{0,6}(失效|过期)|(失效|过期).{0,6}二维码|qr ?code.{0,12}expired/i;
  if (!leaves.some((el) => expired.test(el.textContent) && visible(el))) return null;
  const refresh = leaves.find((el) => /^\\s*(点击)?刷新(二维码)?\\s*$|^\\s*refresh\\s*$/i.test(el.textContent) && visible(el));
  if (refresh) { refresh.click(); return true; }
  return false;
}"""


class LoginProgress:
    """一次“等用户登录”期间的登录状态判断。

    “离开了登录页”不算登录成功（可能只是点了返回、跳去了别的页面）。
    必须有明确的证据：这个网站“登录成功后才会写入”的 cookie，在等待期间新出现或者换了值。
    没有证据就一直是“待确认”，不会自动继续，等用户本人点“已登录，继续”。
    没有专门写过规则的网站（LoginSite.login_cookies 为空）永远没有证据，只能由用户确认。
    """

    def __init__(self, get_cookies):
        """get_cookies：不带参数的函数，返回浏览器当前的全部 cookies（Playwright 的格式）。"""
        self._get_cookies = get_cookies
        # 开始等待时先记下已有的登录 cookie：以前存下来的旧 cookie 可能早就失效了，不能当成这次登录的证据
        cookies = self._cookies()
        # 连“登录前”的 cookie 都没读到，就没法分辨新旧：这次等待一律不给证据，只能由用户确认
        self._no_baseline = cookies is None
        self._before = {site.name: site.login_cookie_values(cookies or []) for site in SITES}

    def _cookies(self) -> list[dict] | None:
        try:
            return self._get_cookies()
        except Exception:
            return None  # 浏览器正在跳转或已经关闭，读不到

    def evidence(self, url: str) -> str | None:
        """有已登录证据就返回一句说明（只写 cookie 名，不写值），没有返回 None。"""
        site = site_for(url)
        if not site.login_cookies or self._no_baseline:
            return None
        before = self._before.get(site.name, {})
        for name, values in site.login_cookie_values(self._cookies() or []).items():
            if values - before.get(name, set()):  # 出现了登录前没有的值
                return f"cookie:{name}"
        return None

    def confirmed(self, url: str) -> bool:
        """可以认定登录成功、不用等用户点按钮：有证据，而且页面已经离开登录页（跳转完成）。"""
        return not is_login_page(url) and self.evidence(url) is not None


class LoginSite:
    """通用做法。某个网站不一样时，写一个子类覆盖对应的属性或方法。"""

    name = "通用"
    hosts: tuple[str, ...] = ()  # 这套做法管哪些域名（含子域名）；空表示兜底
    find_qr_js = FIND_QR_JS
    refresh_qr_js = REFRESH_QR_JS
    # 已登录证据：只有登录成功后网站才会写入的 cookie 名。空表示这个网站没有可靠证据，只能由用户确认
    login_cookies: tuple[str, ...] = ()

    def _owns_host(self, host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def matches(self, url: str) -> bool:
        return self._owns_host(urlparse(url).hostname or "")

    def login_cookie_values(self, cookies: list[dict]) -> dict[str, set[str]]:
        """从浏览器的全部 cookies 里，挑出属于这个网站、有值的登录 cookie：{名字: 所有的值}。
        同一个名字可能在不同子域名、路径下各有一个，所以值是一个集合。"""
        found: dict[str, set[str]] = {}
        for cookie in cookies:
            domain = (cookie.get("domain") or "").lstrip(".")
            if cookie.get("name") in self.login_cookies and cookie.get("value") and self._owns_host(domain):
                found.setdefault(cookie["name"], set()).add(cookie["value"])
        return found

    def is_login_page(self, url: str) -> bool:
        parsed = urlparse(url)
        return any(hint in (parsed.netloc + parsed.path).lower() for hint in LOGIN_URL_HINTS)

    def grab_qr(self, page) -> bytes | None:
        """登录页上的二维码单独截一张清晰的 PNG。
        整页截图缩小后放在网页右边，二维码只剩 80 像素左右，手机扫不出来（实测显示宽度 700 以下就识别失败），
        所以单独截出来、在登录卡片里放大显示。只找主页面，不找 iframe 里的。"""
        try:
            box = page.evaluate(self.find_qr_js)
            if not box:
                return None
            x, y, w, h = box
            pad = 12  # 二维码四周要留白，否则有的扫码器认不出来
            return page.screenshot(
                type="png", clip={"x": max(0, x - pad), "y": max(0, y - pad), "width": w + 2 * pad, "height": h + 2 * pad}
            )
        except Exception:
            return None  # 页面正在跳转之类，跳过这一帧

    def refresh_expired_qr(self, page) -> bool:
        """二维码过期就自动刷新，不用你去点。找不到“刷新”时整页重新加载（登录页重新打开会换一个新二维码）。
        返回 True 表示刷新了。"""
        try:
            result = page.evaluate(self.refresh_qr_js)
            if result is None:
                return False
            if result is False:
                page.reload(wait_until="domcontentloaded")
            print("[登录] 二维码过期，已自动刷新")
            page.wait_for_timeout(800)
            return True
        except Exception:
            return False  # 页面正在跳转之类，下一轮再看


class JDLogin(LoginSite):
    """京东：登录页在 passport.jd.com，扫码登录。二维码查找和过期刷新就是按它调出来的，沿用通用做法。"""

    name = "京东"
    hosts = ("jd.com",)
    # thor：电脑版登录凭证；pt_key：手机版（m.jd.com）登录凭证。
    # 注意：这两个名字还没有用真实扫码登录验证过；如果登录后一直停在“待确认”，先核对这里
    login_cookies = ("thor", "pt_key")


GENERIC = LoginSite()
SITES: tuple[LoginSite, ...] = (JDLogin(),)  # 新网站加在这里


def site_for(url: str) -> LoginSite:
    """这个网址归哪一套登录做法管；没有专门写过的用通用做法。"""
    for site in SITES:
        if site.matches(url):
            return site
    return GENERIC


def is_login_page(url: str) -> bool:
    return site_for(url).is_login_page(url)
