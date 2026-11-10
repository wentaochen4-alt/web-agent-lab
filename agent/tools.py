"""给模型用的自定义工具（官方 computer use 动作之外的）。

函数本身不做事，只是把名字、参数和说明（docstring）告诉模型；
模型调用时，真正的处理在 agent/core.py 的 LoggedAgent.handle_action 里。
docstring 就是给模型看的提示词，改它等于改提示词。
"""


def report_plan_progress(step: int, status: str, note: str = "") -> dict:
    """报告执行计划的进度。开始做某一项、完成某一项、或者某一项做不下去时调用。

    Args:
        step: 计划里的第几项，从 1 开始。
        status: "in_progress" 表示开始做，"done" 表示完成，"failed" 表示做不下去。
        note: 可选，一句话说明，比如失败原因或者收集到的关键信息。
    """
    return {"ok": True}


def request_login(reason: str) -> dict:
    """遇到必须登录才能继续的页面（比如登录页、扫码登录）时调用，请用户本人登录。

    不要自己输入账号、密码或验证码，也不要因为需要登录就结束任务。
    调用后会暂停，等用户登录完成；返回登录后的当前页面，接着完成任务。

    Args:
        reason: 一句话说明为什么需要登录，比如“京东搜索需要先登录”。
    """
    return {"ok": True}


def list_tabs() -> dict:
    """列出浏览器里现在打开的所有标签页：编号、标题、网址，以及哪个是当前标签页。

    你的所有操作和看到的截图都只针对当前标签页。
    点击链接后如果打开了新标签页，会自动切换到新标签页。
    """
    return {"ok": True}


def switch_tab(index: int) -> dict:
    """切换到另一个标签页，返回那个标签页的当前画面。

    Args:
        index: 标签页编号，从 1 开始，以 list_tabs 的结果为准。
    """
    return {"ok": True}


def close_tab(index: int) -> dict:
    """关闭一个不再需要的标签页，返回关闭之后当前标签页的画面。只剩一个标签页时不能关。

    Args:
        index: 标签页编号，从 1 开始，以 list_tabs 的结果为准。
    """
    return {"ok": True}


# 始终注册给模型的自定义工具；新增工具时加在这里，并在 LoggedAgent.handle_action 里处理
CUSTOM_TOOLS = (report_plan_progress, request_login)
# 只有浏览器开了多标签页模式（SandboxedPlaywright(multi_tab=True)）才注册的工具
TAB_TOOLS = (list_tabs, switch_tab, close_tab)


def with_safety_ack(args: dict, result):
    """官方代码只在返回页面状态时带上确认标记。返回普通字典时要自己加，否则下一次请求会报 400。"""
    if isinstance(result, dict) and args.get("safety_decision"):
        return {**result, "safety_acknowledgement": "true"}
    return result
