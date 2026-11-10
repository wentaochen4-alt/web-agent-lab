"""接入 Google 官方参考实现 computer-use-preview（git 子模块，不改它）。

官方代码里也有一个叫 agent 的模块（computer-use-preview/agent.py），和我们的 agent/ 包重名。
所以这里做两件事，其他文件一律从这里拿官方的类，不要自己 import：
1. 把官方目录加到模块搜索路径的“最后”，这样 `import agent` 找到的仍然是我们的包，
   `import computers` 才会落到官方目录。
2. 官方的 agent.py 按文件路径加载，另起一个名字，避免和我们的包冲突。
"""
import importlib.util
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent  # 项目根目录
REPO = LAB / "computer-use-preview"

if str(REPO) not in sys.path:
    sys.path.append(str(REPO))

from dotenv import load_dotenv  # noqa: E402

# Vertex 配置（项目、区域）在官方目录的 .env 里，要在加载官方代码之前读进来
load_dotenv(REPO / ".env")

from computers import EnvState, PlaywrightComputer  # noqa: E402


def _load_official_agent():
    name = "computer_use_preview_agent"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BrowserAgent = _load_official_agent().BrowserAgent

__all__ = ["LAB", "REPO", "BrowserAgent", "EnvState", "PlaywrightComputer"]
