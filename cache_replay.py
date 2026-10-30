"""缓存回放：同一个任务第一次让 Agent 做，记下每一步点了哪个页面元素；之后直接回放，不再调用模型。

流程：
1. 查缓存：同样的任务 + 起始网址有缓存，就直接回放。
2. 回放：按记下的元素逐个操作（找元素，不是靠坐标），最后只调用一次模型，看结果页给出答案并确认任务真的完成了。
3. 自愈：找不到元素或者结果页不对，就让完整的 Agent 重新做一遍，成功后更新缓存。
4. 安全：录制时需要确认的步骤（比如提交），回放时同样要确认。

用法：
  computer-use-preview/.venv/bin/python cache_replay.py --query "任务描述" [--auto_confirm]
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import run_local as core
from google import genai
from google.genai import types

CACHE_DIR = core.LAB / "cache"
REPLAY_TIMEOUT_MS = 3000  # 回放时等元素出现的时间，等不到就算缓存失效
ANSWER_MODEL = "gemini-3.5-flash-lite"  # 回放后读结果页用的模型，越快越好

# 在浏览器里执行：找出坐标 (x, y) 或当前焦点处的元素，取出能用来重新定位它的特征
DESCRIBE_JS = """
([x, y]) => {
  let el = x === null ? document.activeElement : document.elementFromPoint(x, y);
  if (!el || el === document.body) return null;
  el = el.closest('a, button, input, textarea, select, label, [role=button], [onclick]') || el;
  if (el.tagName === 'LABEL' && el.control) el = el.control;
  return {
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    name: el.getAttribute('name'),
    type: el.getAttribute('type'),
    value: el.getAttribute('value'),
    href: el.getAttribute('href'),
    aria: el.getAttribute('aria-label'),
    placeholder: el.getAttribute('placeholder'),
    text: (el.innerText || '').trim().slice(0, 40),
    label: el.labels && el.labels[0] ? el.labels[0].innerText.trim().split('\\n')[0].slice(0, 40) : null,
  };
}
"""


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def selector_candidates(info: dict) -> list[str]:
    """从元素特征生成几个候选选择器，越稳定的越靠前。"""
    tag, cands = info["tag"], []
    if info.get("id"):
        cands.append(f'[id="{_q(info["id"])}"]')
    if tag == "input" and info.get("type") in ("radio", "checkbox") and info.get("name"):
        cands.append(f'input[name="{_q(info["name"])}"][value="{_q(info.get("value") or "")}"]')
    if info.get("name"):
        cands.append(f'{tag}[name="{_q(info["name"])}"]')
    if tag == "a" and info.get("href"):
        cands.append(f'a[href="{_q(info["href"])}"]')
    if info.get("aria"):
        cands.append(f'{tag}[aria-label="{_q(info["aria"])}"]')
    if info.get("placeholder"):
        cands.append(f'{tag}[placeholder="{_q(info["placeholder"])}"]')
    if info.get("text") and tag in ("a", "button"):
        cands.append(f'{tag}:has-text("{_q(info["text"])}")')
    return cands


class RecordingPlaywright(core.SandboxedPlaywright):
    """在 Agent 操作的同时，把每一步翻译成可回放的“配方”步骤。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.recipe: list[dict] = []
        self.cacheable = True
        self.pending_confirm = False  # 下一步是否是用户确认过的关键操作

    def _locate(self, x: int | None, y: int | None) -> tuple[str | None, str]:
        info = self._page.evaluate(DESCRIBE_JS, [x, y])
        if not info:
            return None, ""
        # 给人看的名字：优先用输入框旁边的说明文字，其次按钮上的字，最后才是代码里的字段名
        desc = (
            info.get("label")
            or info.get("aria")
            or info.get("text")
            or info.get("placeholder")
            or info.get("name")
            or info["tag"]
        )
        for sel in selector_candidates(info):
            if self._page.locator(sel).count() == 1:  # 只用能唯一定位到这个元素的选择器
                return sel, desc
        return None, desc

    def _add(self, step: dict):
        if self.pending_confirm:
            step["needs_confirm"] = True
            self.pending_confirm = False
        self.recipe.append(step)

    def click_at(self, x, y):
        sel, desc = self._locate(x, y)
        state = super().click_at(x, y)
        self._add({"op": "click", "selector": sel, "x": x, "y": y, "desc": desc})
        return state

    def type_text(self, text, press_enter=False):
        sel, desc = self._locate(None, None)
        state = super().type_text(text, press_enter)
        self._add({"op": "fill", "selector": sel, "text": text, "enter": press_enter, "desc": desc})
        return state

    def type_text_at(self, x, y, text, press_enter=False, clear_before_typing=True):
        sel, desc = self._locate(x, y)
        state = super().type_text_at(x, y, text, press_enter, clear_before_typing)
        self._add({"op": "fill", "selector": sel, "x": x, "y": y, "text": text, "enter": press_enter, "desc": desc})
        return state

    def navigate(self, url):
        state = super().navigate(url)
        self._add({"op": "goto", "url": state.url})
        return state

    def key_combination(self, keys):
        state = super().key_combination(keys)
        self._add({"op": "keys", "keys": list(keys)})
        return state

    def go_back(self):
        state = super().go_back()
        self._add({"op": "back"})
        return state

    def drag_and_drop(self, *args, **kwargs):
        self.cacheable = False  # 拖拽回放不可靠，这类任务不进缓存
        return super().drag_and_drop(*args, **kwargs)


class RecordingAgent(core.LoggedAgent):
    def _get_safety_confirmation(self, safety):
        result = super()._get_safety_confirmation(safety)
        if result == "CONTINUE":
            self._browser_computer.pending_confirm = True
        return result


class CacheMiss(Exception):
    pass


def cache_path(query: str, start_url: str) -> Path:
    key = hashlib.sha1(f"{query.strip()}|{start_url}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{key}.json"


def confirm(step: dict, auto_confirm: bool) -> bool:
    if auto_confirm:
        return True
    if not sys.stdin.isatty():
        print(f"[需要确认，已停止] 回放到关键操作：{step.get('desc')}")
        return False
    return input(f"回放到关键操作“{step.get('desc')}”，继续吗？[y/N] ").strip().lower() in ("y", "yes")


def describe_step(step: dict) -> str:
    """把配方步骤写成给人看的一句话（网页版拿它当计划显示）。"""
    op, desc = step["op"], step.get("desc") or ""
    if op == "click":
        return f"点击“{desc}”" if desc else "点击"
    if op == "fill":
        return f"在“{desc}”填写“{step['text']}”" if desc else f"输入“{step['text']}”"
    if op == "goto":
        return f"打开 {step['url']}"
    if op == "keys":
        return "按键 " + " + ".join(step["keys"])
    if op == "back":
        return "返回上一页"
    return op


def replay_steps(
    page,
    steps: list[dict],
    auto_confirm: bool,
    trajectory: core.Trajectory,
    confirm_fn=None,
    before_step=None,
    after_step=None,
) -> bool:
    """按配方逐步操作。返回 False 表示用户没有确认关键操作；找不到元素时抛出 CacheMiss。

    confirm_fn / before_step / after_step 给网页版用：换成网页上的确认卡片、实时显示每一步。
    """
    for i, step in enumerate(steps, 1):
        if step.get("needs_confirm"):
            ok = confirm_fn(step) if confirm_fn else confirm(step, auto_confirm)
            if not ok:
                return False
        if before_step:
            before_step(i, step)
        op, sel = step["op"], step.get("selector")
        try:
            if op in ("click", "fill") and sel:
                target = page.locator(sel)
                target.wait_for(state="visible", timeout=REPLAY_TIMEOUT_MS)
                if op == "click":
                    target.click(timeout=REPLAY_TIMEOUT_MS)
                else:
                    target.fill(step["text"], timeout=REPLAY_TIMEOUT_MS)
                    if step.get("enter"):
                        target.press("Enter")
            elif op in ("click", "fill"):
                # 录制时没找到稳定的选择器，只能按坐标兜底
                if "x" in step:
                    page.mouse.click(step["x"], step["y"])
                if op == "fill":
                    page.keyboard.type(step["text"])
                    if step.get("enter"):
                        page.keyboard.press("Enter")
            elif op == "goto":
                page.goto(step["url"])
            elif op == "keys":
                keys = [core_key(k) for k in step["keys"]]
                page.keyboard.press("+".join(keys))
            elif op == "back":
                page.go_back()
            page.wait_for_load_state()
        except Exception as e:
            raise CacheMiss(f"第 {i} 步（{describe_step(step)}）找不到对应的页面元素") from e
        trajectory.log({"event": "replay_step", "index": i, "step": step})
        if after_step:
            after_step(i, step)
    return True


def core_key(key: str) -> str:
    from computers.playwright.playwright import PLAYWRIGHT_KEY_MAP

    return PLAYWRIGHT_KEY_MAP.get(key.lower(), key)


def read_answer(query: str, page_text: str, screenshot: bytes, model: str) -> str | None:
    """回放完只调用一次模型：看结果页回答任务，同时判断任务是否真的完成。

    优先发页面文字（比截图小得多，实测 1~3 秒）；页面没有文字时才发截图。
    """
    client = genai.Client(
        vertexai=os.environ.get("USE_VERTEXAI", "0").lower() in ("true", "1"),
        project=os.environ.get("VERTEXAI_PROJECT"),
        location=os.environ.get("VERTEXAI_LOCATION"),
    )
    prompt = (
        f"任务：{query}\n\n下面是执行完这个任务之后的浏览器页面。"
        "如果页面显示任务已经完成，直接根据页面内容简洁地回答任务里要求的信息；"
        "如果页面显示任务没有完成或出错了，只回答 FAILED。"
    )
    if page_text.strip():
        page = types.Part(text="页面文字：\n" + page_text[:8000])
    else:
        page = types.Part.from_bytes(data=screenshot, mime_type="image/png")
    response = client.models.generate_content(
        model=model,
        contents=[types.Part(text=prompt), page],
        config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level="MINIMAL")),
    )
    text = (response.text or "").strip()
    return None if not text or text.upper().startswith("FAILED") else text


def run_agent_and_record(args, allowed, trajectory) -> tuple[str, str | None, dict | None]:
    with RecordingPlaywright(
        trajectory=trajectory,
        allowed_domains=allowed,
        allow_all=args.allow_all,
        screen_size=core.SCREEN_SIZE,
        initial_url=args.initial_url,
    ) as computer:
        agent = RecordingAgent(
            trajectory=trajectory,
            auto_confirm=args.auto_confirm,
            thinking_level=args.thinking,
            browser_computer=computer,
            query=args.query,
            model_name=args.model,
            verbose=False,
        )
        agent.attach_initial_page(computer.current_state())
        status = agent.run(args.max_steps, args.max_minutes)
        recipe = None
        if status == "COMPLETE" and computer.cacheable and computer.recipe:
            recipe = make_recipe(args.query, args.initial_url, computer.recipe, trajectory.run_dir.name)
        return status, agent.final_answer, recipe


def make_recipe(query: str, start_url: str, steps: list[dict], source_run: str) -> dict:
    return {
        "query": query.strip(),
        "start_url": start_url,
        "steps": steps,
        "created": datetime.now().isoformat(timespec="seconds"),
        "source_run": source_run,
    }


def save_recipe(recipe: dict) -> Path:
    path = cache_path(recipe["query"], recipe["start_url"])
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def try_replay(args, allowed, trajectory, recipe) -> tuple[str, str | None]:
    with core.SandboxedPlaywright(
        trajectory=trajectory,
        allowed_domains=allowed,
        allow_all=args.allow_all,
        screen_size=core.SCREEN_SIZE,
        initial_url=args.initial_url,
    ) as computer:
        if not replay_steps(computer._page, recipe["steps"], args.auto_confirm, trajectory):
            return "SAFETY_STOP", None
        final = computer.current_state()
        page_text = computer._page.inner_text("body")
    answer = read_answer(args.query, page_text, final.screenshot, args.answer_model)
    if not answer:
        raise CacheMiss("回放完成了，但结果页看起来不对")
    return "COMPLETE", answer


def main() -> int:
    parser = argparse.ArgumentParser(description="带缓存回放的浏览器 Agent")
    parser.add_argument("--query", required=True)
    parser.add_argument("--initial_url", default="http://127.0.0.1:8770/index.html")
    parser.add_argument("--model", default="gemini-3.6-flash")
    parser.add_argument("--answer_model", default=ANSWER_MODEL, help="回放后读结果页用的模型，越快越好")
    parser.add_argument("--thinking", choices=("minimal", "low", "medium", "high"), default="low")
    parser.add_argument("--max_steps", type=int, default=25)
    parser.add_argument("--max_minutes", type=float, default=10)
    parser.add_argument("--auto_confirm", action="store_true")
    parser.add_argument("--allow", nargs="*", default=[])
    parser.add_argument("--allow_all", action="store_true")
    parser.add_argument("--no_cache", action="store_true", help="忽略已有缓存，强制让 Agent 重做并更新缓存")
    args = parser.parse_args()

    allowed = tuple(args.allow)
    path = cache_path(args.query, args.initial_url)
    trajectory = core.Trajectory(core.LAB / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    trajectory.log({"event": "start", "query": args.query, "model": args.model, "initial_url": args.initial_url})
    started = time.time()
    mode, status, answer = "AGENT", None, None

    if path.exists() and not args.no_cache:
        recipe = json.loads(path.read_text(encoding="utf-8"))
        print(f"[缓存命中] 回放 {len(recipe['steps'])} 步（录制于 {recipe['created']}）")
        try:
            status, answer = try_replay(args, allowed, trajectory, recipe)
            mode = "REPLAY"
        except CacheMiss as e:
            print(f"[缓存失效] {e}，改由 Agent 重新完成并更新缓存")
            trajectory.log({"event": "cache_miss", "reason": str(e)})
            mode = "HEALED"

    if status is None:
        status, answer, recipe = run_agent_and_record(args, allowed, trajectory)
        if recipe:
            path = save_recipe(recipe)
            print(f"[已写入缓存] {len(recipe['steps'])} 步 → {path.name}")

    seconds = round(time.time() - started, 1)
    trajectory.log({"event": "end", "status": status, "final_answer": answer, "mode": mode, "seconds": seconds})
    print(f"\n模式：{mode}　状态：{status}　耗时：{seconds} 秒\n回答：{answer}")
    print(f"运行记录：{trajectory.run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
