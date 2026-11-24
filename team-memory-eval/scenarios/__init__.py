"""任务的公开视图与宿主持有的材料、评分配置分开保存。"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CACHE = ROOT / "experiments/.cache"
SOURCES = {
    "marble": ("ulab-uiuc/MARBLE", "eae9f15e5b9f1e808f8ff61800b129d5538c1b47"),
    "drbench": ("ServiceNow/drbench", "0d699ecf6aa96b1de378595b432e9b16a82f0ed9"),
}


def reference_repo(benchmark):
    upstream, revision = SOURCES[benchmark]
    parent = Path(os.environ.get("TME_REFERENCE_REPOS", ROOT / "experiments/upstream"))
    repo = parent / upstream.split("/")[-1]
    if not (repo / ".git").exists():
        raise FileNotFoundError(f"缺少参考仓库 {repo}，请检出 {upstream} 的 {revision}")
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != revision:
        raise ValueError(f"{repo} 版本不匹配：当前 {head}，任务要求 {revision}")
    return repo.resolve()


def official_package(benchmark):
    """从任务固定版本的参考仓库导入官方工具和评分器。"""
    repo = reference_repo(benchmark)
    deps = Path(
        os.environ.get("TME_BENCHMARK_DEPS", ROOT / "experiments/benchmark-deps")
    )
    if deps.is_dir() and str(deps) not in sys.path:
        sys.path.insert(0, str(deps))
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    return repo


@dataclass
class Task:
    benchmark: str
    id: str
    revision: str
    goal: str
    format: str
    roles: list
    resources: dict
    outputs: dict
    owners: dict = field(default_factory=dict)
    materials: dict = field(default_factory=dict)
    operations: dict = field(default_factory=dict)
    reference: dict = field(default_factory=dict, repr=False)
    role_source: str = "upstream"
    operation_descriptions: dict = field(default_factory=dict)

    def public(self):
        return {
            "benchmark": self.benchmark,
            "id": self.id,
            "goal": self.goal,
            "format": self.format,
            "resources": self.resources,
            "outputs": self.outputs,
            "owners": self.owners,
        }


def load(benchmark, task_id):
    from scenarios import drbench, marble

    def read_manifest(name):
        spec = yaml.safe_load((ROOT / "tasks" / name).read_text())
        if spec["revision"] != SOURCES[benchmark][1]:
            raise ValueError("任务版本不匹配")
        return spec

    if benchmark not in SOURCES:
        raise ValueError(f"未知基准：{benchmark}")
    data_root = reference_repo(benchmark)

    def fetch(path):
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("无效的上游路径")
        file = data_root / relative
        if not file.is_file():
            raise FileNotFoundError(f"缺少任务文件 {file}，请检查参考仓库的检出范围")
        return file

    if benchmark == "marble":
        match = re.fullmatch(r"research/([1-9][0-9]*)", task_id)
        if not match:
            raise ValueError(
                f"MARBLE 题号应为 research/N（N 为正整数），收到 {task_id!r}"
            )
        spec = read_manifest("marble/research.yaml")
        spec["upstream_id"] = int(match[1])
        return marble.load(spec, fetch)
    if not re.fullmatch(r"DR[0-9]{4}", task_id):
        raise ValueError(f"DRBench 题号应为 DR 加四位数字，收到 {task_id!r}")
    spec = read_manifest("drbench/research.yaml")
    spec["upstream_id"] = task_id
    return drbench.load(spec, fetch)
