"""浏览器 Agent 的本地网页版后端。

复用 run_local.py 里的 Agent（白名单、日志、卡住检测都在那里），
这里只负责：从网页接任务、把每一步实时推给网页、等网页上的确认按钮、列出历史记录。

启动：
  cd ~/Desktop/web-agent-lab
  computer-use-preview/.venv/bin/python -m uvicorn app.server:app --port 8780
"""
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import cache_replay as cache
import run_local as core

STATIC = Path(__file__).resolve().parent / "static"
RUNS = core.LAB / "runs"
RUN_ID = re.compile(r"^\d{8}-\d{6}$")
STEP_FILE = re.compile(r"^(step_\d+\.png|live\.jpg)$")
LIVE_INTERVAL = 1.5  # 等人处理时，每隔几秒刷新一次实时画面

MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
THINKING = ["minimal", "low", "medium", "high"]


class RunRequest(BaseModel):
    query: str
    start_url: str = "http://127.0.0.1:8770/index.html"
    model: str = MODELS[0]
    thinking: str = "low"
    auto_confirm: bool = False
    plan: bool = True  # 先生成计划，让用户确认或修改后再执行
    use_cache: bool = True  # 同样的任务做成功过，就直接回放，不调用模型
    max_steps: int = 25
    max_minutes: float = 10


class Decision(BaseModel):
    value: str  # continue / cancel
    plan: list[str] | None = None  # 确认计划时，带上用户改过的步骤


class PlanUpdate(BaseModel):
    steps: list[str]


def _clean_steps(steps: list[str] | None) -> list[str]:
    return [s.strip() for s in steps or [] if s and s.strip()][:12]


class RunState:
    """一次运行在内存里的状态：事件列表（推给网页）和等待中的确认。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.done = False
        self.agent: "UIAgent | None" = None
        self._decision = "cancel"
        self.decision_plan: list[str] | None = None
        self._decision_ready = threading.Event()
        self.awaiting = False

    def push(self, record: dict):
        with self.cond:
            self.events.append(record)
            self.cond.notify_all()

    def finish(self):
        with self.cond:
            self.done = True
            self.cond.notify_all()

    def wait_for_decision(self, tick=None) -> str:
        """等网页上的按钮。给了 tick 就每隔 LIVE_INTERVAL 秒调用它一次（刷新实时画面），
        tick 返回 True 表示不用等按钮了，直接当作“继续”。"""
        self._decision_ready.clear()
        self.awaiting = True
        while not self._decision_ready.wait(timeout=LIVE_INTERVAL if tick else None):
            if tick():
                self.decide("continue")
        self.awaiting = False
        return self._decision

    def decide(self, value: str, plan: list[str] | None = None):
        self._decision = value
        self.decision_plan = plan
        self._decision_ready.set()

    def stop(self):
        if self.agent:
            self.agent.stop_requested = True
        if self.awaiting:
            self.decide("cancel")


class LiveTrajectory(core.Trajectory):
    """照常写 trajectory.jsonl，同时把每条记录推给正在看的网页。"""

    def __init__(self, run_dir: Path, state: RunState):
        super().__init__(run_dir)
        self._state = state

    def log(self, record: dict):
        super().log(record)
        self._state.push(record)


class UIAgent(core.LoggedAgent):
    """把命令行里的“按回车 / 输入 q”换成网页上的按钮。"""

    def __init__(self, state: RunState, **kwargs):
        super().__init__(**kwargs)
        self._state = state

    def _ask_user(self, kind: str, text: str, action: str | None = None, live: bool = False, done_when=None) -> str:
        """在网页上弹卡片等你决定。live=True 时一边等一边把浏览器画面实时推给网页；
        done_when(网址) 返回 True 时不用等按钮，自动继续。"""
        self._trajectory.log({"event": "awaiting", "kind": kind, "text": text, "action": action})
        tick = None
        if live:
            frame = 0

            def tick() -> bool:
                nonlocal frame
                page = self._browser_computer._page
                try:
                    shot = page.screenshot(type="jpeg", quality=70)
                except Exception:
                    return False  # 截图偶尔超时，跳过这一帧
                (self._trajectory.run_dir / "live.jpg").write_bytes(shot)
                frame += 1
                # 实时画面只推给正在看的网页，不写进 trajectory.jsonl（太多了）
                self._state.push({"event": "live", "n": frame, "url": page.url})
                return bool(done_when and done_when(page.url))

            tick()  # 先推一帧，不用等第一个间隔
        value = self._state.wait_for_decision(tick)
        self._trajectory.log({"event": "decision", "kind": kind, "value": value})
        return value

    def _pending_intent(self, safety: dict) -> str | None:
        # 需要确认的那个动作还没执行，从模型这一轮的输出里找出它的说明文字
        for part in self._contents[-1].parts or []:
            call = part.function_call
            if call and call.args and call.args.get("safety_decision") == safety:
                return call.args.get("intent")
        return None

    def _get_safety_confirmation(self, safety):
        if self._auto_confirm:
            return super()._get_safety_confirmation(safety)
        value = self._ask_user("confirm", safety.get("explanation", ""), self._pending_intent(safety))
        if value == "continue":
            return "CONTINUE"
        self.safety_stopped = True
        return "TERMINATE"

    def _wait_for_human(self) -> bool:
        text = "页面一直没有变化。你可以在弹出的浏览器窗口里手动处理（比如关掉弹窗），处理完点“继续”。"
        return self._ask_user("stuck", text, live=True) == "continue"

    def _wait_for_login(self, reason: str) -> bool:
        text = "请在右边的实时画面里登录（比如用手机 App 扫码），登录成功后会自动继续。登录状态会保存在本机，下次不用再登。"
        # 先到过登录页、后来又离开了，就当作登录成功。
        # （模型可能在还没跳到登录页时就喊人登录，不能一开始就判成功）
        seen_login_page = False

        def done(url: str) -> bool:
            nonlocal seen_login_page
            if core.is_login_page(url):
                seen_login_page = True
                return False
            return seen_login_page

        return self._ask_user("login", text, reason, live=True, done_when=done) == "continue"

    def plan_with_user(self, initial: core.EnvState) -> bool:
        """先让模型出计划，等用户在网页上确认或修改；用户取消就返回 False。"""
        self._trajectory.log({"event": "planning"})
        steps = self.make_plan(initial)
        self._trajectory.log({"event": "plan", "steps": steps, "source": "model"})
        if self._ask_user("plan", "确认或修改计划后开始执行") != "continue":
            return False
        self.set_plan(_clean_steps(self._state.decision_plan) or steps)
        return True


class UIRecordingAgent(cache.PlanFailureMixin, UIAgent):
    """边做边录制：你在网页上确认过的关键操作会记进配方，回放时同样要确认。"""

    def _get_safety_confirmation(self, safety):
        result = super()._get_safety_confirmation(safety)
        if result == "CONTINUE":
            self._browser_computer.pending_confirm = True
        return result


class ReplayHandle:
    """回放期间占住 state.agent：“停止”按钮照样生效；回放没有计划可以中途修改。"""

    stop_requested = False
    plan = None


class ReplayStopped(Exception):
    pass


def _replay(state: RunState, trajectory: LiveTrajectory, req: RunRequest, recipe: dict) -> tuple[str, str | None]:
    """按缓存的配方回放，每一步都推给网页；找不到元素或结果不对时抛出 CacheMiss。"""
    steps = recipe["steps"]
    handle = ReplayHandle()
    state.agent = handle
    trajectory.log({"event": "cache_hit", "steps": len(steps), "created": recipe.get("created")})
    trajectory.log({"event": "plan", "steps": [cache.describe_step(s) for s in steps], "source": "cache"})

    with core.SandboxedPlaywright(
        trajectory=trajectory,
        allow_all=True,
        screen_size=core.SCREEN_SIZE,
        initial_url=req.start_url,
    ) as computer:
        page = computer._page
        computer.current_state()  # 起始页截图

        def confirm(step):
            if req.auto_confirm:
                trajectory.log({"event": "safety_auto_confirm", "safety": {"explanation": "回放中的关键操作"}})
                return True
            text = "这一步在录制时需要你确认，回放时同样需要确认。"
            trajectory.log({"event": "awaiting", "kind": "confirm", "text": text, "action": cache.describe_step(step)})
            value = state.wait_for_decision()
            trajectory.log({"event": "decision", "kind": "confirm", "value": value})
            return value == "continue"

        def before(i, step):
            if handle.stop_requested:
                raise ReplayStopped()
            trajectory.log({"event": "plan_progress", "step": i, "status": "in_progress"})

        def after(i, step):
            # 不走 current_state（它固定等 0.5 秒），直接截图，让回放保持快
            trajectory.step += 1
            name = f"step_{trajectory.step:02d}.png"
            (trajectory.run_dir / name).write_bytes(page.screenshot(type="png"))
            trajectory.log({"event": "plan_progress", "step": i, "status": "done"})
            trajectory.log({"event": "screenshot", "step": trajectory.step, "url": page.url, "file": name})

        try:
            done = cache.replay_steps(
                page, steps, req.auto_confirm, trajectory, confirm_fn=confirm, before_step=before, after_step=after
            )
        except ReplayStopped:
            return "STOPPED", None
        if not done:
            return "SAFETY_STOP", None
        page_text = page.inner_text("body")
        shot = page.screenshot(type="png")

    answer = cache.read_answer(req.query, page_text, shot, cache.ANSWER_MODEL)
    if not answer:
        raise cache.CacheMiss("回放完成了，但结果页看起来不对")
    return "COMPLETE", answer


app = FastAPI(title="Web Agent")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_lock = threading.Lock()
_active: RunState | None = None
_states: dict[str, RunState] = {}


def _run_job(state: RunState, req: RunRequest):
    global _active
    trajectory = LiveTrajectory(RUNS / state.run_id, state)
    trajectory.log(
        {
            "event": "start",
            "query": req.query,
            "model": req.model,
            "thinking": req.thinking,
            "initial_url": req.start_url,
            "allowed_domains": "ALL",
        }
    )
    status, answer, mode = "ERROR", None, "AGENT"
    try:
        recipe_path = cache.cache_path(req.query, req.start_url)
        if req.use_cache and recipe_path.exists():
            try:
                status, answer = _replay(state, trajectory, req, json.loads(recipe_path.read_text(encoding="utf-8")))
                mode = "REPLAY"
            except cache.CacheMiss as e:
                # 页面变了：交给 Agent 重新做，成功后覆盖旧缓存
                trajectory.log({"event": "cache_miss", "reason": str(e)})
                mode = "HEALED"
    except Exception as e:
        trajectory.log({"event": "error", "message": str(e)[:500]})
        mode = "REPLAY"  # 回放出了别的错，不再交给 Agent，直接结束
    try:
        if mode != "REPLAY":
            status, answer = _run_agent(state, trajectory, req)
    except Exception as e:  # 出错也要把状态告诉网页，不能让它一直转圈
        trajectory.log({"event": "error", "message": str(e)[:500]})
    trajectory.log({"event": "end", "status": status, "final_answer": answer, "mode": mode})
    (RUNS / state.run_id / "result.txt").write_text(
        f"状态：{status}\n最终回答：{answer}\n", encoding="utf-8"
    )
    state.finish()
    with _lock:
        _active = None


def _run_agent(state: RunState, trajectory: LiveTrajectory, req: RunRequest) -> tuple[str, str | None]:
    """让 Agent 完成任务，同时录制每一步点了哪个元素；做成功了就写入缓存，下次直接回放。"""
    status, answer = "ERROR", None
    # 网页版不限制网站；提交、购买这类关键操作仍然会弹卡片问你
    with cache.RecordingPlaywright(
        trajectory=trajectory,
        allow_all=True,
        screen_size=core.SCREEN_SIZE,
        initial_url=req.start_url,
    ) as computer:
        agent = UIRecordingAgent(
            state=state,
            trajectory=trajectory,
            auto_confirm=req.auto_confirm,
            thinking_level=req.thinking,
            browser_computer=computer,
            query=req.query,
            model_name=req.model,
            verbose=False,
        )
        state.agent = agent
        initial = computer.current_state()
        agent.attach_initial_page(initial)
        if req.plan and not agent.plan_with_user(initial):
            status = "CANCELLED"
        else:
            status = agent.run(req.max_steps, req.max_minutes)
            answer = agent.final_answer
        if status == "COMPLETE" and computer.cacheable and computer.recipe:
            page = computer._page
            ok, reason = cache.check_success(
                req.query, answer, page.url, page.inner_text("body"), agent.failed_plan_steps
            )
            if ok:
                recipe = cache.make_recipe(req.query, req.start_url, computer.recipe, state.run_id)
                cache.save_recipe(recipe)
                trajectory.log({"event": "cache_saved", "steps": len(recipe["steps"])})
            else:
                trajectory.log({"event": "cache_skipped", "reason": reason})
    return status, answer


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config():
    return {"models": MODELS, "thinking": THINKING, "local_origin": core.ALLOWED_ORIGINS[0]}


@app.get("/api/runs")
def list_runs():
    runs = []
    for path in sorted(RUNS.glob("*/trajectory.jsonl"), reverse=True):
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            continue
        first, last = json.loads(lines[0]), json.loads(lines[-1])
        run_id = path.parent.name
        active = _active is not None and _active.run_id == run_id
        runs.append(
            {
                "id": run_id,
                "query": first.get("query", ""),
                "time": first.get("time"),
                "status": "RUNNING" if active else (last.get("status") if last.get("event") == "end" else "UNKNOWN"),
            }
        )
    return runs[:50]


@app.post("/api/runs")
def start_run(req: RunRequest):
    global _active
    req.query = req.query.strip()
    if not req.query:
        raise HTTPException(400, "任务不能为空")
    if req.model not in MODELS or req.thinking not in THINKING:
        raise HTTPException(400, "模型或思考深度不在可选范围内")
    if urlparse(req.start_url).scheme not in ("http", "https"):
        raise HTTPException(400, "起始网址要以 http:// 或 https:// 开头")
    with _lock:
        if _active is not None:
            raise HTTPException(409, "已经有一个任务在运行")
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        state = RunState(run_id)
        _active = state
        _states[run_id] = state
    threading.Thread(target=_run_job, args=(state, req), daemon=True).start()
    return {"id": run_id}


def _check_id(run_id: str):
    if not RUN_ID.match(run_id) or not (RUNS / run_id).is_dir():
        raise HTTPException(404, "找不到这次运行")


def _sse(record: dict) -> str:
    return f"data: {json.dumps(record, ensure_ascii=False, default=str)}\n\n"


@app.get("/api/runs/{run_id}/events")
def events(run_id: str):
    _check_id(run_id)
    state = _states.get(run_id)

    def from_file():
        for line in (RUNS / run_id / "trajectory.jsonl").read_text(encoding="utf-8").splitlines():
            yield f"data: {line}\n\n"
        yield _sse({"event": "__eof"})

    def live():
        sent = 0
        while True:
            with state.cond:
                if sent >= len(state.events) and not state.done:
                    state.cond.wait(timeout=15)
                batch = state.events[sent:]
                sent = len(state.events)
                finished = state.done
            for record in batch:
                yield _sse(record)
            if finished and sent >= len(state.events):
                yield _sse({"event": "__eof"})
                return
            if not batch:
                yield ": keepalive\n\n"

    stream = live() if state else from_file()
    return StreamingResponse(stream, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/runs/{run_id}/files/{name}")
def run_file(run_id: str, name: str):
    _check_id(run_id)
    if not STEP_FILE.match(name):
        raise HTTPException(404)
    return FileResponse(RUNS / run_id / name)


@app.post("/api/runs/{run_id}/decision")
def decide(run_id: str, decision: Decision):
    state = _states.get(run_id)
    if not state or not state.awaiting:
        raise HTTPException(409, "这次运行现在不需要确认")
    state.decide("continue" if decision.value == "continue" else "cancel", decision.plan)
    return {"ok": True}


@app.post("/api/runs/{run_id}/plan")
def update_plan(run_id: str, update: PlanUpdate):
    """运行中改计划：下一轮开始前交给模型。"""
    state = _states.get(run_id)
    steps = _clean_steps(update.steps)
    if not state or state.done or not state.agent or not state.agent.plan:
        raise HTTPException(409, "这次运行现在不能改计划")
    if not steps:
        raise HTTPException(400, "计划至少要有一步")
    state.agent.pending_plan = steps
    return {"ok": True}


@app.post("/api/runs/{run_id}/stop")
def stop(run_id: str):
    state = _states.get(run_id)
    if not state or state.done:
        raise HTTPException(409, "这次运行已经结束")
    state.stop()
    return {"ok": True}
