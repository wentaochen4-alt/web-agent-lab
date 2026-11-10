"""一次运行在内存里的状态：推给网页的事件、等待中的确认、排队的接管操作。

跑 Agent 的线程和处理网页请求的线程都会用到它，所以这里的方法要注意加锁。
"""
import threading
import time

LIVE_INTERVAL = 1.5  # 等人处理时，每隔几秒刷新一次实时画面


class RunState:
    """一次运行在内存里的状态：事件列表（推给网页）和等待中的确认。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.done = False
        self.agent = None  # 正在跑的 UIAgent；缓存回放时是 ReplayHandle（都在 app/server.py）
        self._decision = "cancel"
        self.decision_plan: list[str] | None = None
        self.decision_note: str | None = None
        self._decision_ready = threading.Event()
        self.awaiting = False
        self.awaiting_kind: str | None = None  # 正在等什么：plan / confirm / stuck / login / pause
        # 接管：网页上点击、打字先排队，由跑 Agent 的线程在刷新画面时执行（Playwright 不能跨线程调用）
        self._inputs: list[dict] = []
        self._keep_inputs = False  # 排着的操作是“点画面要求接管”时留下的，下一次等待开始时不要清掉
        self._wake = threading.Event()  # 有新的接管操作或做了决定时，叫醒等待循环，不用等满 LIVE_INTERVAL
        self.last_input_time = 0.0  # 最后一次接管操作的时间（time.monotonic）

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
        with self.cond:
            if not self._keep_inputs:
                self._inputs.clear()  # 上一次等待时没来得及执行的操作已经过时了
            self._keep_inputs = False
        self.awaiting = True
        if tick:
            self._wake.set()  # 马上刷新第一帧，不用等第一个间隔
        try:
            while not self._decision_ready.is_set():
                self._wake.wait(timeout=LIVE_INTERVAL if tick else None)
                self._wake.clear()
                if self._decision_ready.is_set():
                    break
                if tick and tick():
                    self.decide("continue")
        finally:
            # tick 里出了意外也要复位，否则网页会以为还在等这一次的决定
            self.awaiting = False
            self.awaiting_kind = None
        return self._decision

    def decide(self, value: str, plan: list[str] | None = None, note: str | None = None):
        self._decision = value
        self.decision_plan = plan
        self.decision_note = note
        self._decision_ready.set()
        self._wake.set()

    def add_input(self, record: dict, keep: bool = False):
        """keep=True：Agent 还在执行时点的（点画面就接管），留到它停下来后再执行。"""
        with self.cond:
            self._inputs = self._inputs[-19:] + [record]  # 最多排 20 个，防止狂点堆积
            self._keep_inputs = self._keep_inputs or keep
            self.last_input_time = time.monotonic()
        self._wake.set()

    def take_inputs(self) -> list[dict]:
        with self.cond:
            batch, self._inputs = self._inputs, []
        return batch

    def stop(self):
        if self.agent:
            self.agent.stop_requested = True
        if self.awaiting:
            self.decide("cancel")
