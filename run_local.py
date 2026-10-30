"""在本地测试网站上运行 Gemini 浏览器 Agent（基于 google/computer-use-preview）。

相比官方的 main.py，多做了三件事：
1. 网络白名单：浏览器只能访问本地测试站，其他请求一律拦截。
2. 轨迹日志：每一步的截图、动作、网址都存到 runs/<时间>/ 下。
3. 安全默认值：模型要求“需要用户确认”时直接停止；超过最大步数也停止。

用法：
  computer-use-preview/.venv/bin/python run_local.py --query "任务描述" --highlight_mouse
"""
import argparse
import functools
import hashlib
import ipaddress
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

LAB = Path(__file__).resolve().parent
REPO = LAB / "computer-use-preview"
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

from agent import BrowserAgent  # noqa: E402
from computers import EnvState, PlaywrightComputer  # noqa: E402
from google.genai import types  # noqa: E402

SCREEN_SIZE = (1440, 900)
STUCK_WINDOW = 3  # 连续几步页面毫无变化（或重复同一个动作）就算卡住
ALLOWED_ORIGINS = ("http://127.0.0.1:8770", "http://localhost:8770")
# 部署在服务器上时打开：不让 Agent 访问服务器本机和内网的其他服务（本地测试站除外）
BLOCK_PRIVATE_NETWORK = os.environ.get("BLOCK_PRIVATE_NETWORK", "").lower() in ("1", "true")


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


class Trajectory:
    """把每一步写进 trajectory.jsonl，截图存成 step_XX.png。"""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.step = 0
        run_dir.mkdir(parents=True, exist_ok=True)

    def log(self, record: dict):
        record["time"] = round(time.time(), 2)
        with open(self.run_dir / "trajectory.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class SandboxedPlaywright(PlaywrightComputer):
    """官方的 Playwright 浏览器，加上网络白名单和截图记录。"""

    def __init__(
        self,
        trajectory: Trajectory,
        allowed_domains: tuple[str, ...] = (),
        allow_all: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._trajectory = trajectory
        self._allowed_domains = allowed_domains
        self._allow_all = allow_all  # True 时不限制网站，只靠“需要确认就问人”来保护
        self.recent_states: list[tuple[str, str]] = []  # (截图哈希, 网址)，用来判断有没有卡住

    def is_allowed(self, url: str) -> bool:
        if url.startswith(ALLOWED_ORIGINS):
            return True
        host = urlparse(url).hostname or ""
        if BLOCK_PRIVATE_NETWORK and host and is_private_host(host):
            return False
        if self._allow_all:
            return True
        # 允许域名本身和它的子域名，比如 wikipedia.org 也放行 en.wikipedia.org
        return any(host == d or host.endswith("." + d) for d in self._allowed_domains)

    def __enter__(self):
        super().__enter__()
        # 白名单之外的请求全部拦截，Agent 就跑不出本地测试站；服务器上还要拦住内网
        if not self._allow_all or BLOCK_PRIVATE_NETWORK:
            self._context.route("**/*", self._guard)
        return self

    def _guard(self, route):
        url = route.request.url
        if self.is_allowed(url):
            route.continue_()
        else:
            print(f"[已拦截] {url}")
            self._trajectory.log({"event": "blocked", "url": url})
            route.abort()

    def navigate(self, url: str) -> EnvState:
        # 跳转到白名单外的网址：不报错退出，留在原页面，让模型自己换办法
        target = url if url.startswith(("http://", "https://")) else "https://" + url
        if not self.is_allowed(target):
            print(f"[已拦截跳转] {target}")
            self._trajectory.log({"event": "blocked", "url": target, "via": "navigate"})
            return self.current_state()
        return super().navigate(url)

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


PLAN_PROMPT = (
    "你是浏览器操作助手的规划器。根据用户的任务和当前页面截图，把任务拆成 2 到 8 个具体步骤。"
    "每一步是一句简短的中文，写要达成的小目标（不要写坐标），按执行顺序排列；"
    "最后一步通常是“把结果告诉用户”。只输出 JSON 字符串数组。"
)


def report_plan_progress(step: int, status: str, note: str = "") -> dict:
    """报告执行计划的进度。开始做某一项、完成某一项、或者某一项做不下去时调用。

    Args:
        step: 计划里的第几项，从 1 开始。
        status: "in_progress" 表示开始做，"done" 表示完成，"failed" 表示做不下去。
        note: 可选，一句话说明，比如失败原因或者收集到的关键信息。
    """
    return {"ok": True}


def _numbered(steps: list[str]) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))


class LoggedAgent(BrowserAgent):
    """官方的 Agent 循环，加上动作记录、安全停止、最大步数和执行计划。"""

    def __init__(
        self,
        trajectory: Trajectory,
        auto_confirm: bool = False,
        thinking_level: str = "low",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._trajectory = trajectory
        self._auto_confirm = auto_confirm
        self.safety_stopped = False
        self.stop_requested = False  # 网页版的“停止”按钮会把它设为 True
        self.final_answer: str | None = None
        self._recent_actions: list[str] = []
        self._last_logged_turn = None
        # 思考越浅每一步越快；默认 low，难任务可以调成 medium / high
        self._generate_content_config.thinking_config = types.ThinkingConfig(
            include_thoughts=True, thinking_level=thinking_level.upper()
        )
        self.plan: list[str] = []
        self.pending_plan: list[str] | None = None  # 运行中用户改了计划，下一轮开始前生效
        # 官方代码的第二个工具组放自定义函数，把“报告计划进度”加进去
        self._generate_content_config.tools[1].function_declarations.append(
            types.FunctionDeclaration.from_callable(client=self._client, callable=report_plan_progress)
        )

    def make_plan(self, state: EnvState) -> list[str]:
        """动手之前单独问一次模型：看着起始页，把任务拆成几步。"""
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=f"{PLAN_PROMPT}\n\n任务：{self._query}\n当前网址：{state.url}"),
                        types.Part.from_bytes(data=state.screenshot, mime_type="image/png"),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=list[str],
                thinking_config=types.ThinkingConfig(thinking_level="LOW"),
            ),
        )
        steps = response.parsed if isinstance(response.parsed, list) else json.loads(response.text or "[]")
        return [str(s).strip() for s in steps if str(s).strip()][:8]

    def set_plan(self, steps: list[str]):
        """把用户确认过的计划交给模型，并要求它边做边汇报进度。"""
        self.plan = steps
        self._contents[0].parts.append(
            types.Part(
                text="下面是用户确认过的执行计划，请按顺序执行：\n"
                + _numbered(steps)
                + "\n每开始做一项、完成一项、或者某一项做不下去时，调用 report_plan_progress 报告（step 从 1 开始）。"
                "计划只是参考：如果页面实际情况和计划不一样，可以灵活处理，但要在 note 里说明。"
            )
        )
        self._trajectory.log({"event": "plan", "steps": steps, "source": "confirmed"})

    def _apply_pending_plan(self):
        steps, self.pending_plan = self.pending_plan, None
        self.plan = steps
        self._add_to_last_turn(
            types.Part(
                text="用户修改了执行计划。已经完成的不用重做，接下来按新计划执行：\n"
                + _numbered(steps)
                + "\n继续用 report_plan_progress 报告进度，step 按新计划的编号。"
            )
        )
        self._trajectory.log({"event": "plan", "steps": steps, "source": "edited"})

    def attach_initial_page(self, state: EnvState):
        """官方代码第一轮只发文字任务，模型看不到页面只能先摸索；这里把起始页截图一起发过去。"""
        self._contents[0].parts.extend(
            [
                types.Part(text=f"浏览器已经打开了 {state.url}，下面是当前页面的截图。"),
                types.Part.from_bytes(data=state.screenshot, mime_type="image/png"),
            ]
        )

    def _parts_of_last_turn(self, thought: bool) -> list[str]:
        # 只看模型自己说的话；最后一轮如果是我们发的提示，就不算回答
        if self._contents[-1].role != "model":
            return []
        parts = self._contents[-1].parts or []
        return [p.text for p in parts if p.text and bool(p.thought) == thought]

    def handle_action(self, action, use_legacy_actions):
        args = dict(action.args or {})
        # 同一轮里可能有多个动作，模型的思考只记一次
        if self._last_logged_turn is not self._contents[-1]:
            self._last_logged_turn = self._contents[-1]
            thoughts = self._parts_of_last_turn(thought=True)
            if thoughts:
                self._trajectory.log({"event": "thought", "text": "\n".join(thoughts)})
        if action.name == report_plan_progress.__name__:
            # 只是汇报进度，不操作页面，也不算进卡住检测
            self._trajectory.log(
                {
                    "event": "plan_progress",
                    "step": int(args.get("step", 0)),
                    "status": args.get("status"),
                    "note": args.get("note", ""),
                }
            )
            # 模型有时会把 safety_decision 之类的附加字段也塞进来，只取需要的
            return report_plan_progress(
                step=int(args.get("step", 0)), status=str(args.get("status", "")), note=str(args.get("note", ""))
            )
        self._trajectory.log({"event": "action", "name": action.name, "args": args})
        # intent 是模型写的说明文字，每次措辞不同，比较“是不是同一个动作”时去掉
        key = {k: v for k, v in args.items() if k not in ("intent", "safety_decision")}
        self._recent_actions.append(action.name + json.dumps(key, sort_keys=True, default=str))
        return super().handle_action(action, use_legacy_actions)

    def _is_stuck(self) -> bool:
        states = self._browser_computer.recent_states[-STUCK_WINDOW:]
        same_page = len(states) == STUCK_WINDOW and len(set(states)) == 1
        actions = self._recent_actions[-STUCK_WINDOW:]
        same_action = len(actions) == STUCK_WINDOW and len(set(actions)) == 1
        return same_page or same_action

    def _reset_stuck_tracking(self):
        self._browser_computer.recent_states.clear()
        self._recent_actions.clear()

    def _add_to_last_turn(self, *parts: types.Part):
        # 提示单独作为一条用户消息发给模型。
        # 不能塞进“工具返回结果”那一轮里，否则 Gemini 经常直接回一个空回复。
        self._contents.append(types.Content(role="user", parts=list(parts)))

    def _nudge(self):
        print("[卡住了] 页面连续几步没有变化，提醒模型换个办法")
        self._trajectory.log({"event": "stuck_nudge"})
        self._add_to_last_turn(
            types.Part(
                text="提示：最近几步操作后页面没有任何变化，你可能卡住了。"
                "不要重复同样的操作，换一种办法（比如滚动找别的入口、返回上一页、换个链接）。"
                "如果遇到验证码、登录、注册等必须由人处理的情况，直接说明原因并结束任务，不要继续尝试。"
            )
        )
        self._reset_stuck_tracking()

    def _wait_for_human(self) -> bool:
        """命令行版：在终端里等人处理完按回车。网页版会覆盖这个方法，改成等网页上的按钮。"""
        if not sys.stdin.isatty():
            print("[卡住了] 提醒后仍然没有进展，非交互运行，停止")
            return False
        answer = input(
            "\n[卡住了] Agent 没有进展。请在弹出的浏览器窗口里手动处理"
            "（比如登录、关掉弹窗），处理完按回车让它继续；输入 q 结束：\n"
        )
        return answer.strip().lower() != "q"

    def _ask_human(self) -> bool:
        """卡住且提醒无效：请人在浏览器窗口里接管，处理完把当前页面交回给模型。"""
        self._trajectory.log({"event": "stuck_needs_human"})
        if not self._wait_for_human():
            return False
        state = self._browser_computer.current_state()
        self._trajectory.log({"event": "human_takeover_done", "url": state.url})
        self._add_to_last_turn(
            types.Part(text="用户已经在浏览器里手动处理了卡住的地方。下面是当前页面，请从这里继续完成任务。"),
            types.Part.from_bytes(data=state.screenshot, mime_type="image/png"),
        )
        self._reset_stuck_tracking()
        return True

    def _get_safety_confirmation(self, safety):
        # 默认遇到需要用户确认的动作一律停止；只有显式加了 --auto_confirm 才放行
        if self._auto_confirm:
            print(f"[需要确认，已自动放行] {safety.get('explanation')}")
            self._trajectory.log({"event": "safety_auto_confirm", "safety": safety})
            return "CONTINUE"
        print(f"[需要确认，已停止] {safety.get('explanation')}")
        self._trajectory.log({"event": "safety_stop", "safety": safety})
        self.safety_stopped = True
        return "TERMINATE"

    def run(self, max_steps: int, max_minutes: float) -> str:
        deadline = time.time() + max_minutes * 60
        nudged = False
        asked_for_answer = False
        for _ in range(max_steps):
            if self.stop_requested:
                return "STOPPED"
            if self.pending_plan is not None:
                self._apply_pending_plan()
            if time.time() > deadline:
                print(f"超过时长上限 {max_minutes} 分钟，停止")
                return "TIMEOUT"
            if self.run_one_iteration() == "COMPLETE":
                if self.stop_requested:
                    return "STOPPED"
                if self.safety_stopped:
                    return "SAFETY_STOP"
                # 最后一轮里不是“思考”的文字才是给用户的回答
                self.final_answer = "\n".join(self._parts_of_last_turn(thought=False)).strip() or None
                if self.final_answer:
                    return "COMPLETE"
                if not asked_for_answer:
                    # Gemini 偶尔做完了却回一个空回复：追问一次，让它把结果说出来
                    asked_for_answer = True
                    self._trajectory.log({"event": "ask_for_answer"})
                    self._add_to_last_turn(
                        types.Part(
                            text="你还没有给出最终回答。请根据当前页面，直接用文字回答任务要求的结果；"
                            "如果任务没有完成，说明卡在哪里。"
                        )
                    )
                    continue
                # 追问过还是什么都没说，多半是放弃了，不算完成
                return "GAVE_UP"
            if self._is_stuck():
                if not nudged:
                    # 第一次卡住：提醒模型换办法
                    self._nudge()
                    nudged = True
                elif self._ask_human():
                    # 提醒过还卡住：人工接管后重新计数
                    nudged = False
                else:
                    return "STUCK"
        print(f"达到最大步数 {max_steps}，停止")
        return "MAX_STEPS"


def main() -> int:
    parser = argparse.ArgumentParser(description="在本地测试站上运行浏览器 Agent")
    parser.add_argument("--query", required=True, help="交给 Agent 的任务")
    parser.add_argument("--initial_url", default="http://127.0.0.1:8770/index.html")
    parser.add_argument("--model", default="gemini-3.6-flash")
    parser.add_argument("--max_steps", type=int, default=25)
    parser.add_argument("--max_minutes", type=float, default=10, help="整次运行的时长上限（分钟）")
    parser.add_argument(
        "--thinking",
        choices=("minimal", "low", "medium", "high"),
        default="low",
        help="模型每一步的思考深度，越浅越快",
    )
    parser.add_argument("--highlight_mouse", action="store_true", help="在点击位置画红圈，方便观看")
    parser.add_argument(
        "--auto_confirm",
        action="store_true",
        help="模型要求用户确认时自动放行（只适合本地测试站，白名单保证它出不去）",
    )
    parser.add_argument(
        "--allow",
        nargs="*",
        default=[],
        help="额外放行的外部域名（含子域名），例如 --allow wikipedia.org wikimedia.org",
    )
    parser.add_argument("--allow_all", action="store_true", help="不限制网站，任何网址都能访问")
    args = parser.parse_args()

    allowed_domains = tuple(args.allow)
    host = urlparse(args.initial_url).hostname or ""
    if not (
        args.allow_all
        or args.initial_url.startswith(ALLOWED_ORIGINS)
        or any(host == d or host.endswith("." + d) for d in allowed_domains)
    ):
        sys.exit(f"initial_url 必须在白名单内：{ALLOWED_ORIGINS} 或 --allow 的域名")

    run_dir = LAB / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    trajectory = Trajectory(run_dir)
    trajectory.log(
        {
            "event": "start",
            "query": args.query,
            "model": args.model,
            "initial_url": args.initial_url,
            "allowed_domains": "ALL" if args.allow_all else allowed_domains,
        }
    )

    with SandboxedPlaywright(
        trajectory=trajectory,
        allowed_domains=allowed_domains,
        allow_all=args.allow_all,
        screen_size=SCREEN_SIZE,
        initial_url=args.initial_url,
        highlight_mouse=args.highlight_mouse,
    ) as computer:
        agent = LoggedAgent(
            trajectory=trajectory,
            auto_confirm=args.auto_confirm,
            thinking_level=args.thinking,
            browser_computer=computer,
            query=args.query,
            model_name=args.model,
        )
        agent.attach_initial_page(computer.current_state())
        status = agent.run(args.max_steps, args.max_minutes)

    trajectory.log({"event": "end", "status": status, "final_answer": agent.final_answer})
    (run_dir / "result.txt").write_text(
        f"状态：{status}\n最终回答：{agent.final_answer}\n", encoding="utf-8"
    )
    print(f"\n运行记录保存在：{run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
