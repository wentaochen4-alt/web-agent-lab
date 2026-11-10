"""LoggedAgent：在官方 Agent 循环外面加上主循环控制、执行计划、卡住检测和请人接管。

这里的逻辑对所有网站、所有任务类型通用（搜索、浏览、整理信息、填表、后台操作……），
不要写进任何只对某个网站或某类任务成立的判断。
"""
import json
import sys
import time

from google.genai import types

from .login import LoginProgress, is_login_page
from .tools import (
    CUSTOM_TOOLS,
    TAB_TOOLS,
    close_tab,
    list_tabs,
    report_plan_progress,
    request_login,
    switch_tab,
    with_safety_ack,
)
from .trajectory import Trajectory
from .upstream import BrowserAgent, EnvState

STUCK_WINDOW = 3  # 连续几步页面毫无变化（或重复同一个动作）就算卡住

PLAN_PROMPT = (
    "你是浏览器操作助手的规划器。根据用户的任务和当前页面截图，把任务拆成 2 到 8 个具体步骤。"
    "每一步是一句简短的中文，写要达成的小目标（不要写坐标），按执行顺序排列；"
    "最后一步通常是“把结果告诉用户”。只输出 JSON 字符串数组。"
)


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
        self.pause_requested = False  # 网页版的“暂停”按钮会把它设为 True，当前这一步做完就停下
        self.human_note: str | None = None  # 接管结束时，用户留给模型的话
        self.human_wait_seconds = 0.0  # 等人花掉的时间，不算进运行时长上限
        self._model_error: Exception | None = None  # 官方代码吞掉的模型请求错误
        self.final_answer: str | None = None
        self.plan_status: dict[int, str] = {}  # 计划每一项最新汇报的状态
        self._login_declined = False  # 用户说过不登录，就不再因为登录页暂停
        self._recent_actions: list[str] = []
        self._last_logged_turn = None
        self._pending_hints: list[str] = []  # 这一轮动作做完后要告诉模型的话（单独一条消息发）
        self._skipped_actions = 0  # 因为用户要接管而没有执行的动作数
        # 思考越浅每一步越快；默认 low，难任务可以调成 medium / high
        self._generate_content_config.thinking_config = types.ThinkingConfig(
            include_thoughts=True, thinking_level=thinking_level.upper()
        )
        self.plan: list[str] = []
        self.pending_plan: list[str] | None = None  # 运行中用户改了计划，下一轮开始前生效
        # 官方代码的第二个工具组放自定义函数，把我们的自定义工具（agent/tools.py）加进去。
        # 标签页工具只在浏览器开了多标签页模式时才告诉模型
        self._tab_tools = TAB_TOOLS if getattr(self._browser_computer, "multi_tab", False) else ()
        for func in CUSTOM_TOOLS + self._tab_tools:
            self._generate_content_config.tools[1].function_declarations.append(
                types.FunctionDeclaration.from_callable(client=self._client, callable=func)
            )

    # ---------- 执行计划 ----------

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

    # ---------- 和模型的对话 ----------

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

    def _add_to_last_turn(self, *parts: types.Part):
        # 提示单独作为一条用户消息发给模型。
        # 不能塞进“工具返回结果”那一轮里，否则 Gemini 经常直接回一个空回复。
        self._contents.append(types.Content(role="user", parts=list(parts)))

    def get_model_response(self, *args, **kwargs):
        # 官方代码请求失败后不报错，直接当作“完成”。这里记下错误，让 run 能发现
        try:
            return super().get_model_response(*args, **kwargs)
        except Exception as e:
            self._model_error = e
            raise

    # ---------- 执行动作 ----------

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
            self.plan_status[int(args.get("step", 0))] = str(args.get("status"))
            # 模型有时会把 safety_decision 之类的附加字段也塞进来，只取需要的
            return with_safety_ack(
                args,
                report_plan_progress(
                    step=int(args.get("step", 0)), status=str(args.get("status", "")), note=str(args.get("note", ""))
                ),
            )
        if action.name == request_login.__name__:
            return with_safety_ack(args, self._handle_login_request(str(args.get("reason", ""))))
        if self.pause_requested:
            # 用户已经要求暂停 / 接管：模型这一轮想做的动作不再执行，原样把当前页面交回去，
            # 主循环马上会停下来等人。这样接管不用等它把这一步做完
            self._skipped_actions += 1
            self._trajectory.log({"event": "action_skipped", "name": action.name})
            return self._browser_computer.current_state()
        self._trajectory.log({"event": "action", "name": action.name, "args": args})
        # intent 是模型写的说明文字，每次措辞不同，比较“是不是同一个动作”时去掉
        key = {k: v for k, v in args.items() if k not in ("intent", "safety_decision")}
        self._recent_actions.append(action.name + json.dumps(key, sort_keys=True, default=str))
        if action.name in {func.__name__ for func in self._tab_tools}:
            result = self._handle_tab_tool(action.name, args)
            if isinstance(result, dict):
                return with_safety_ack(args, result)
        else:
            result = super().handle_action(action, use_legacy_actions)
        failed_url = getattr(self._browser_computer, "failed_navigation", None)
        if failed_url:
            self._browser_computer.failed_navigation = None
            self._pending_hints.append(
                f"提示：刚才要打开的网址 {failed_url} 打不开（超时或连接失败），页面还停在原来的地方。"
                "不要再试这个网址，换别的办法，比如回到搜索结果点击链接，或者换一个网址。"
            )
        # 实测模型到了登录页不会主动调用 request_login，而是想办法绕开（换移动版、换搜索引擎）。
        # 所以由程序判断：动作做完停在登录页，就直接暂停请用户登录
        if isinstance(result, EnvState) and is_login_page(result.url) and not self._login_declined:
            return with_safety_ack(args, self._handle_login_request("打开的页面需要登录"))
        return result

    def _handle_tab_tool(self, name: str, args: dict):
        """标签页工具（多标签页模式）。切换、关闭成功时返回页面状态，官方代码会把截图交给模型。"""
        browser = self._browser_computer
        if name == list_tabs.__name__:
            return {"tabs": browser.tabs()}
        try:
            index = int(args.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        state = browser.switch_tab(index) if name == switch_tab.__name__ else browser.close_tab(index)
        if state is None:
            if name == close_tab.__name__ and len(browser.tabs()) < 2:
                return {"ok": False, "message": "只剩一个标签页，不能关闭。"}
            return {"ok": False, "message": f"没有第 {index} 个标签页。先调用 list_tabs 看看有哪些标签页。"}
        return state

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

    # ---------- 卡住检测 ----------

    def _is_stuck(self) -> bool:
        states = self._browser_computer.recent_states[-STUCK_WINDOW:]
        same_page = len(states) == STUCK_WINDOW and len(set(states)) == 1
        actions = self._recent_actions[-STUCK_WINDOW:]
        same_action = len(actions) == STUCK_WINDOW and len(set(actions)) == 1
        return same_page or same_action

    def _reset_stuck_tracking(self):
        self._browser_computer.recent_states.clear()
        self._recent_actions.clear()

    def _nudge(self):
        print("[卡住了] 页面连续几步没有变化，提醒模型换个办法")
        self._trajectory.log({"event": "stuck_nudge"})
        self._add_to_last_turn(
            types.Part(
                text="提示：最近几步操作后页面没有任何变化，你可能卡住了。"
                "不要重复同样的操作，换一种办法（比如滚动找别的入口、返回上一页、换个链接）。"
                "如果需要登录，调用 request_login 请用户登录；"
                "如果遇到验证码、注册等其他必须由人处理的情况，直接说明原因并结束任务，不要继续尝试。"
            )
        )
        self._reset_stuck_tracking()

    # ---------- 等人处理：卡住、登录、暂停 ----------
    # 三个 _wait_for_* 是命令行版的做法（终端里按回车）；网页版在 app/server.py 里覆盖成网页按钮

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

    def _wait_for_login(self, reason: str, progress: LoginProgress) -> bool:
        """命令行版：在弹出的浏览器窗口里登录，登录完按回车。网页版会覆盖成实时画面 + 按钮。
        progress 用来判断是不是真的登录了；命令行版只等用户按回车，用不到它。"""
        if not sys.stdin.isatty():
            print("[需要登录] 非交互运行，没法等人登录")
            return False
        answer = input(f"\n[需要登录] {reason}\n请在弹出的浏览器窗口里登录，登录完按回车让它继续；输入 q 结束：\n")
        return answer.strip().lower() != "q"

    def _wait_for_pause(self, reason: str) -> bool:
        """命令行版：在终端里等人处理完按回车。网页版会覆盖这个方法，改成实时画面 + 按钮。"""
        if not sys.stdin.isatty():
            print(f"[暂停] {reason}，非交互运行，没法等人处理，停止")
            return False
        answer = input(f"\n[暂停] {reason}\n请在弹出的浏览器窗口里手动处理，处理完按回车让它继续；输入 q 结束：\n")
        return answer.strip().lower() != "q"

    def _handle_login_request(self, reason: str):
        """模型请用户登录：暂停等人登录。登录成功就保存登录状态，把登录后的页面交回给模型。"""
        self._trajectory.log({"event": "login_request", "reason": reason})
        # 开始等之前先建好：它要记下“登录前”的 cookie，之后才能分辨出这次登录新写入的
        progress = LoginProgress(self._browser_computer.cookies)
        if not self._wait_for_login(reason, progress):
            self._trajectory.log({"event": "login_declined"})
            self._login_declined = True
            return {"ok": False, "message": "用户没有登录。请说明任务卡在需要登录这一步，然后结束任务。"}
        self._browser_computer.save_login_state()
        state = self._browser_computer.current_state()
        # evidence：凭什么认为登录好了。cookie:xxx 是程序查到的证据；user 是没有查到证据、用户本人确认的
        self._trajectory.log({"event": "login_done", "url": state.url, "evidence": progress.evidence(state.url) or "user"})
        self._reset_stuck_tracking()
        # 返回页面状态：官方代码会把网址和截图一起交给模型
        return state

    def _hand_back(self, message: str):
        """人处理完了：把当前页面和说明交回给模型，让它接着做。"""
        # 人手做的步骤没有录下来，这次的做法回放不出来，不能进缓存
        self._browser_computer.cacheable = False
        state = self._browser_computer.current_state()
        self._trajectory.log({"event": "human_takeover_done", "url": state.url})
        if self.human_note:
            message += f"\n用户补充说明：{self.human_note}"
            self.human_note = None
        self._add_to_last_turn(
            types.Part(text=message),
            types.Part.from_bytes(data=state.screenshot, mime_type="image/png"),
        )
        self._reset_stuck_tracking()

    def _ask_human(self) -> bool:
        """卡住且提醒无效：请人在浏览器窗口里接管，处理完把当前页面交回给模型。"""
        self._trajectory.log({"event": "stuck_needs_human"})
        if not self._wait_for_human():
            return False
        self._hand_back("用户已经在浏览器里手动处理了卡住的地方。下面是当前页面，请从这里继续完成任务。")
        return True

    def _pause_for_human(self, reason: str = "") -> bool:
        """暂停让人接管。reason 是出错原因，用户主动暂停时为空。人选择结束就返回 False。"""
        self.pause_requested = False
        skipped, self._skipped_actions = self._skipped_actions, 0
        self._trajectory.log({"event": "paused", "reason": reason})
        if reason:
            # 出错时页面可能还卡在加载中，先停掉，否则实时画面和后面的截图都会超时
            stop_loading = getattr(self._browser_computer, "stop_loading", None)
            if stop_loading:
                stop_loading()
        if not self._wait_for_pause(reason):
            return False
        if reason:
            # 出错的这一轮可能不完整（动作没有结果、结果格式不对），留着下次请求还会被拒绝，退回到这一轮之前
            while len(self._contents) > 1 and self._contents.pop().role != "model":
                pass
            message = f"上一步运行时出了错（{reason}），可能没有做完。用户已经看过浏览器。下面是当前页面，请先确认页面状态，再继续完成任务。"
        elif skipped:
            message = (
                "用户暂停了任务并接管了浏览器，你上一步想做的动作没有执行。"
                "用户可能在浏览器里手动操作过。下面是当前页面，请以当前页面为准，继续完成任务。"
            )
        else:
            message = "用户暂停了任务，可能在浏览器里手动操作过。下面是当前页面，请以当前页面为准，继续完成任务。"
        self._hand_back(message)
        return True

    # ---------- 主循环 ----------

    def _run_step(self) -> str:
        outcome = self.run_one_iteration()
        if self._model_error is not None:
            error, self._model_error = self._model_error, None
            raise error
        return outcome

    def run(self, max_steps: int, max_minutes: float) -> str:
        deadline = time.time() + max_minutes * 60
        self.human_wait_seconds = 0.0
        nudged = False
        asked_for_answer = False
        for _ in range(max_steps):
            if self.stop_requested:
                return "STOPPED"
            if self.pause_requested:
                if not self._pause_for_human():
                    return "STOPPED"
                nudged = False
            if self.pending_plan is not None:
                self._apply_pending_plan()
            if time.time() - self.human_wait_seconds > deadline:
                print(f"超过时长上限 {max_minutes} 分钟，停止")
                return "TIMEOUT"
            try:
                outcome = self._run_step()
            except Exception as e:
                # 出错了不直接结束：停在当前页面等人看，浏览器不关
                if self._pause_for_human(f"这一步出错了：{str(e)[:200]}"):
                    nudged = False
                    continue
                return "STOPPED" if self.stop_requested else "ERROR"
            if self._pending_hints and outcome != "COMPLETE":
                # 动作的结果已经发给模型了，提示要另起一条消息（不能塞进工具返回那一轮）
                hints, self._pending_hints = self._pending_hints, []
                self._add_to_last_turn(types.Part(text="\n".join(hints)))
            if outcome == "COMPLETE":
                if self.stop_requested:
                    return "STOPPED"
                if self.safety_stopped:
                    return "SAFETY_STOP"
                # 最后一轮里不是“思考”的文字才是给用户的回答
                self.final_answer = "\n".join(self._parts_of_last_turn(thought=False)).strip() or None
                if self.final_answer:
                    # 模型给了回答，但计划里还有某一项最后报的是“做不下去”：不能算完成
                    if "failed" in self.plan_status.values():
                        return "INCOMPLETE"
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
