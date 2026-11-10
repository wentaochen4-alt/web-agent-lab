"""命令行入口：运行 Gemini 浏览器 Agent（基于 google/computer-use-preview）。

Agent 本身在 agent/ 包里，这里只负责解析参数、打开浏览器、跑一次任务、写下结果。
相比官方的 main.py，多了三样（实现都在 agent/）：
1. 网络白名单：默认只能访问本地示例站，--allow 放行指定域名，--allow_all 不限制。
2. 轨迹日志：每一步的截图、动作、网址都存到 runs/<时间>/ 下。
3. 安全默认值：模型要求“需要用户确认”时直接停止；超过最大步数也停止。

用法：
  computer-use-preview/.venv/bin/python run_local.py --query "任务描述" --highlight_mouse
"""
import argparse
import sys
from datetime import datetime
from urllib.parse import urlparse

from agent.core import LoggedAgent
from agent.sandbox import ALLOWED_ORIGINS, SCREEN_SIZE, SandboxedPlaywright, host_in_domains, is_local_site
from agent.trajectory import Trajectory
from agent.upstream import LAB


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
    parser.add_argument(
        "--single_tab",
        action="store_true",
        help="关闭多标签页：网页想开新标签页时并入当前页（官方代码原来的做法）",
    )
    args = parser.parse_args()

    allowed_domains = tuple(args.allow)
    host = urlparse(args.initial_url).hostname or ""
    if not (args.allow_all or is_local_site(args.initial_url) or host_in_domains(host, allowed_domains)):
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
        multi_tab=not args.single_tab,
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
