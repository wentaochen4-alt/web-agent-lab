"""浏览器访问约束：网站白名单、禁止访问内网、保存登录状态，以及带这些约束的 SandboxedPlaywright。"""
import functools
import hashlib
import ipaddress
import json
import os
import socket
from urllib.parse import urlparse

from .trajectory import Trajectory
from .upstream import LAB, EnvState, PlaywrightComputer

SCREEN_SIZE = (1440, 900)
# 本地示例站：8770 会议室预订，8771 模拟商城（sites/shop）。它们只是示例场景，始终放行
ALLOWED_ORIGINS = (
    "http://127.0.0.1:8770", "http://localhost:8770",
    "http://127.0.0.1:8771", "http://localhost:8771",
)
# 部署在服务器上时打开：不让 Agent 访问服务器本机和内网的其他服务（本地示例站除外）
BLOCK_PRIVATE_NETWORK = os.environ.get("BLOCK_PRIVATE_NETWORK", "").lower() in ("1", "true")
# 用户登录过的网站，登录状态（cookies）存在这里，下次运行自动带上。只在本机，不进 git
AUTH_STATE = LAB / "auth" / "storage_state.json"
MAX_TABS = 8  # 多标签页模式下最多同时开几个标签页；再多就并入当前页（防止网页不停弹窗口）


def is_local_site(url: str) -> bool:
    """网址是不是本地示例站。拆开后按“协议 + 主机 + 端口”逐项比对，不能只比字符串开头：
    否则 http://127.0.0.1:87701 会被当成 8770，http://127.0.0.1:8770@内网地址 也会混过去
    （@ 前面那段其实是用户名，真正访问的是 @ 后面的主机）。"""
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:  # 端口不是合法数字之类的畸形网址
        return False
    if u.username is not None or u.password is not None:
        return False
    for origin in ALLOWED_ORIGINS:
        o = urlparse(origin)
        if (u.scheme, u.hostname, port) == (o.scheme, o.hostname, o.port):
            return True
    return False


@functools.lru_cache(maxsize=512)
def is_private_host(host: str) -> bool:
    """域名解析到本机、内网或保留地址就算内网。解析失败的不算（浏览器自己也打不开）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def host_in_domains(host: str, domains: tuple[str, ...]) -> bool:
    """允许域名本身和它的子域名，比如 wikipedia.org 也放行 en.wikipedia.org。"""
    return any(host == d or host.endswith("." + d) for d in domains)


class SandboxedPlaywright(PlaywrightComputer):
    """官方的 Playwright 浏览器，加上网络白名单和截图记录；可选多标签页。"""

    def __init__(
        self,
        trajectory: Trajectory,
        allowed_domains: tuple[str, ...] = (),
        allow_all: bool = False,
        multi_tab: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._trajectory = trajectory
        self._allowed_domains = allowed_domains
        self._allow_all = allow_all  # True 时不限制网站，只靠“需要确认就问人”来保护
        # False（默认）：和官方代码一样，网页想开新标签页时并入当前页，始终只有一个标签页。
        # True：保留新标签页并切过去，模型可以用 list_tabs / switch_tab / close_tab 工具
        self.multi_tab = multi_tab
        self._closing = False
        self.failed_navigation: str | None = None  # 刚刚没打开的网址；LoggedAgent 读完会清空
        self.recent_states: list[tuple[str, str]] = []  # (截图哈希, 网址)，用来判断有没有卡住

    def is_allowed(self, url: str) -> bool:
        if is_local_site(url):
            return True
        host = urlparse(url).hostname or ""
        if BLOCK_PRIVATE_NETWORK and host and is_private_host(host):
            return False
        if self._allow_all:
            return True
        return host_in_domains(host, self._allowed_domains)

    def __enter__(self):
        super().__enter__()
        # 以前登录过：把保存的 cookies 放进浏览器，再刷新起始页，让它以登录身份打开
        # （官方代码在 __enter__ 里已经打开了起始页，没法在那之前传登录状态，所以这里补一次刷新）
        if AUTH_STATE.exists():
            cookies = json.loads(AUTH_STATE.read_text(encoding="utf-8")).get("cookies", [])
            if cookies:
                self._context.add_cookies(cookies)
                self._page.reload()
        # 白名单之外的请求全部拦截，Agent 就跑不出允许的网站；服务器上还要拦住内网
        # （拦截挂在整个浏览器上，所以对后来打开的标签页同样有效）
        if not self._allow_all or BLOCK_PRIVATE_NETWORK:
            self._context.route("**/*", self._guard)
        if self.multi_tab:
            self._page.on("close", self._handle_page_closed)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._closing = True  # 关浏览器时标签页会一个个关掉，不用再记录和切换
        return super().__exit__(exc_type, exc_val, exc_tb)

    # ---------- 多标签页（multi_tab=True 时才用到） ----------
    # 官方代码的所有操作都作用在 self._page 上，所以“切换标签页”就是把 self._page 换成另一个页面

    def _handle_new_page(self, new_page):
        """网页打开了新标签页（比如点了“在新窗口打开”的链接）。"""
        if not self.multi_tab or len(self._context.pages) > MAX_TABS:
            return super()._handle_new_page(new_page)  # 官方做法：关掉新标签页，在当前页打开同一个网址
        self.cacheable = False  # 缓存回放只会在一个标签页里操作，用到多标签页的做法回放不出来
        new_page.on("close", self._handle_page_closed)
        self._page = new_page
        self._bring_to_front()
        try:
            # 刚打开时网址可能还是 about:blank，稍等它开始加载，记录和截图才对得上
            new_page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        print(f"[新标签页] {new_page.url}")
        self._trajectory.log({"event": "tab_opened", "url": new_page.url, "tabs": len(self._context.pages)})

    def _handle_page_closed(self, page):
        """标签页被关掉了（网页自己关的，或者 close_tab 关的）。关的是当前页就换到剩下的最后一个。"""
        if self._closing:
            return
        self._trajectory.log({"event": "tab_closed", "tabs": len(self._open_pages())})
        self._ensure_live_page()

    def _open_pages(self) -> list:
        return [p for p in self._context.pages if not p.is_closed()]

    def _ensure_live_page(self):
        """当前标签页被关掉了：换到剩下的最后一个；一个都不剩（网页把自己关了）就开一个空白页。"""
        if not self._page.is_closed():
            return
        pages = self._open_pages()
        if pages:
            self._page = pages[-1]
        else:
            self._trajectory.log({"event": "tab_reopened"})
            self._page = self._context.new_page()  # 会触发 _handle_new_page，那边也会把它设成当前页
        self._bring_to_front()

    def _bring_to_front(self):
        try:
            self._page.bring_to_front()
        except Exception:
            pass  # 只影响有窗口模式下哪个标签页显示在前面，失败不要紧

    def tabs(self) -> list[dict]:
        """所有打开的标签页：编号（从 1 开始）、标题、网址、是不是当前页。"""
        result = []
        for index, page in enumerate(self._open_pages(), 1):
            try:
                title = page.title()
            except Exception:
                title = ""  # 页面正在跳转
            result.append({"index": index, "title": title[:80], "url": page.url, "current": page is self._page})
        return result

    def switch_tab(self, index: int) -> EnvState | None:
        """切到第 index 个标签页，返回它的当前画面；编号不对返回 None。"""
        pages = self._open_pages()
        if not 1 <= index <= len(pages):
            return None
        self.cacheable = False
        self._page = pages[index - 1]
        self._bring_to_front()
        return self.current_state()

    def close_tab(self, index: int) -> EnvState | None:
        """关掉第 index 个标签页，返回关掉之后当前页的画面；编号不对或只剩一个标签页时返回 None。"""
        pages = self._open_pages()
        if len(pages) < 2 or not 1 <= index <= len(pages):
            return None
        self.cacheable = False
        pages[index - 1].close()
        self._ensure_live_page()
        return self.current_state()

    def cookies(self) -> list[dict]:
        """浏览器当前的全部 cookies（agent/login.py 用它判断是不是真的登录了）。"""
        return self._context.cookies()

    def _guard(self, route):
        url = route.request.url
        if self.is_allowed(url):
            route.continue_()
        else:
            print(f"[已拦截] {url}")
            self._trajectory.log({"event": "blocked", "url": url})
            route.abort()

    def save_login_state(self):
        """用户登录成功后调用：把整个浏览器的 cookies 存下来，文件只有自己能读。"""
        AUTH_STATE.parent.mkdir(exist_ok=True)
        self._context.storage_state(path=AUTH_STATE)
        AUTH_STATE.chmod(0o600)

    def navigate(self, url: str) -> EnvState:
        # 跳转到白名单外的网址：不报错退出，留在原页面，让模型自己换办法
        target = url if url.startswith(("http://", "https://")) else "https://" + url
        if not self.is_allowed(target):
            print(f"[已拦截跳转] {target}")
            self._trajectory.log({"event": "blocked", "url": target, "via": "navigate"})
            return self.current_state()
        try:
            return super().navigate(url)
        except Exception as e:
            # 网址打不开（超时、连不上）：不让整次运行出错。停掉加载，留在能截图的页面上，
            # 把这件事记在 failed_navigation 里，LoggedAgent 会告诉模型换办法
            reason = (str(e).splitlines() or [""])[0][:120]
            print(f"[打不开] {target}：{reason}")
            self._trajectory.log({"event": "navigate_failed", "url": target, "error": reason})
            self.stop_loading()
            self.cacheable = False  # 这一步没做成，这次的做法不能进缓存
            self.failed_navigation = target
            return self.current_state()

    def stop_loading(self):
        """页面卡在“加载中”时让它停下来（相当于点浏览器的 ×）。不停的话截图会一直超时。"""
        try:
            session = self._context.new_cdp_session(self._page)
            session.send("Page.stopLoading")
            session.detach()
        except Exception:
            pass  # 停不了就算了，后面截图失败会走原来的出错处理

    def current_state(self) -> EnvState:
        # 每次动作执行完，官方代码都会调用这里截图，顺便存一份到本地
        try:
            state = super().current_state()
        except Exception as e:
            # 有窗口模式下，窗口被挡住或切到后台时 macOS 可能暂停渲染，导致截图超时：切到前台再试一次
            print(f"[截图失败，重试] {str(e)[:80]}")
            self._trajectory.log({"event": "screenshot_retry", "error": str(e)[:200]})
            self._page.bring_to_front()
            state = super().current_state()
        self.recent_states.append((hashlib.md5(state.screenshot).hexdigest(), state.url))
        self._trajectory.step += 1
        shot = self._trajectory.run_dir / f"step_{self._trajectory.step:02d}.png"
        shot.write_bytes(state.screenshot)
        self._trajectory.log(
            {"event": "screenshot", "step": self._trajectory.step, "url": state.url, "file": shot.name}
        )
        return state
