"""运行记录：每次运行一个目录 runs/<时间戳>/。"""
import json
import time
from pathlib import Path


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
