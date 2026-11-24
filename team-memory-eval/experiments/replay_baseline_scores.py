"""从历史评委记录重算逐题和批次分数。只读文件，不调用模型。"""

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean


def replay(directory):
    rows = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    seen = set()
    groups = defaultdict(list)
    details = []
    for row in rows:
        name, task, status = row["batch"], row["task"], row["execution"]
        if (name, task) in seen:
            raise ValueError(f"重复的题目记录 {name}/{task}")
        seen.add((name, task))
        record = json.loads((directory / row["record"]).read_text(encoding="utf-8"))
        dims = []
        value = None
        if row.get("answer"):
            data = (directory / row["answer"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != row["answer_sha256"]:
                raise ValueError(f"{name}/{task} 答案摘要不匹配")
        if row["scoring"] == "scored":
            if row["method"] == "marble-official":
                if f"research/{record['task_id']}" != task:
                    raise ValueError(f"{name}/{task} 官方记录题号不匹配")
                metrics = record["task_evaluation"]
            else:
                if record.get("task_id", task) != task or record["status"] != "scored":
                    raise ValueError(f"{name}/{task} 评分记录不匹配")
                metrics = record["metrics"]
            if row["benchmark"] == "drbench":
                value = metrics["insights_recall"]
                if type(value) not in (float, int) or not 0 <= value <= 1:
                    raise ValueError(f"{name}/{task} 召回率越界")
            else:
                dims = [metrics[k] for k in ("innovation", "safety", "feasibility")]
                if any(type(v) is not int or not 1 <= v <= 5 for v in dims):
                    raise ValueError(f"{name}/{task} 维度分数越界")
                value = mean(dims)
        elif record["status"] != "not_run":
            raise ValueError(f"{name}/{task} 评分状态不匹配")
        groups[name].append(value)
        details.append((name, task, status, row["scoring"], "/".join(map(str, dims)),
                        "" if value is None else f"{value:.6f}", row["source"]))
    return details, groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="解包后含 index.json 的目录")
    args = parser.parse_args()
    rows, groups = replay(args.directory)
    output = csv.writer(sys.stdout, lineterminator="\n")
    output.writerow(("batch", "task", "execution", "scoring", "innovation/safety/feasibility",
                     "score", "original_score_file"))
    output.writerows(rows)
    for name, scores in groups.items():
        rated = [value for value in scores if value is not None]
        value = f"{mean(rated):.6f}" if rated else "无评分"
        print(f"{name}  已评分 {len(rated)}/{len(scores)}  均分 {value}", file=sys.stderr)


if __name__ == "__main__":
    main()
