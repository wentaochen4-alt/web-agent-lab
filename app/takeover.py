"""接管：把你在网页实时画面上的点击、滚动、打字，换算成浏览器里的操作。

网页那边发来的位置是“占画面宽、高的比例”（0–1），这里换算成浏览器窗口里的像素。
操作不在收到请求的线程里执行：先排进 RunState 的队列，由跑 Agent 的线程执行（Playwright 不能跨线程调用）。
"""
from typing import Literal

from pydantic import BaseModel, Field

from agent.sandbox import SCREEN_SIZE

TAKEOVER_KINDS = ("login", "stuck", "pause")  # 只有这几种等待时允许接管：Agent 本来就停着等人，不会和你抢
TAKEOVER_KEYS = ("Enter", "Backspace", "Tab", "Escape")


class TakeoverInput(BaseModel):
    type: Literal["click", "scroll", "type", "key"]
    x: float = Field(0, ge=0, le=1)  # 点在画面上的位置，占宽度、高度的比例
    y: float = Field(0, ge=0, le=1)
    dy: float = Field(0, ge=-3000, le=3000)  # 滚动距离（像素），正数往下
    text: str = Field("", max_length=200)
    key: str = ""


def to_pixels(x: float, y: float) -> tuple[float, float]:
    """画面上的比例位置 → 浏览器窗口里的像素。"""
    width, height = SCREEN_SIZE
    return x * width, y * height


def run_takeover_input(page, record: dict):
    """把网页上的一次接管操作在浏览器里执行。record 是 TakeoverInput 转成的字典。"""
    x, y = to_pixels(record.get("x", 0), record.get("y", 0))
    try:
        if record["type"] == "click":
            page.mouse.click(x, y)
        elif record["type"] == "scroll":
            page.mouse.move(x, y)
            page.mouse.wheel(0, record.get("dy", 0))
        elif record["type"] == "type":
            page.keyboard.type(record.get("text", ""))
        elif record["type"] == "key":
            page.keyboard.press(record["key"])
    except Exception as exc:
        print(f"[接管] 操作没执行成功：{exc}")
