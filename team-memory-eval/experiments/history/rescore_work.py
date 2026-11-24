"""用官方 DRBench 评分器重评一份交付，原运行目录只读。"""
import argparse
import json
import tempfile
from pathlib import Path

import yaml

from eval.score import score
from run import Config
from scenarios import load
from service.gateway import Gateway
from service.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("task")
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    configs = Config.for_tasks(yaml.safe_load(args.config.read_text()))
    config = next((c for c in configs if c.task_id == args.task and c.benchmark == "drbench"), None)
    if config is None:
        parser.error("指定的 DRBench 题不在配置中")
    task = load("drbench", args.task)
    with tempfile.TemporaryDirectory(prefix="tme-rescore-") as tmp:
        store = Store(tmp)
        try:
            values = score(task, {"report.json": args.report.read_bytes()},
                           config.judge_model, Gateway(config, store))
            print(json.dumps({"status":"scored","model":config.judge_model,"metrics":values},
                             ensure_ascii=False, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
