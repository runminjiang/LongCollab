"""TME 实验入口。每道题由独立的环境和一支持久团队执行。"""

import argparse
import asyncio
import json
import multiprocessing
import signal
import re
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

from eval.score import score
from runtime.envs.point_to_point import PointToPointEnv
from runtime.sandbox import Sandbox
from scenarios import ROOT, load
from scenarios.drbench import app_tools
from service import archive, budget, events
from service.api import TeamService
from service.gateway import Gateway, api_base, endpoint_url, is_validation_endpoint
from service.store import Store, write_json

KeyEnv = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    benchmark: Literal["marble", "drbench"]
    task_id: str
    runner: Literal["tme", "codex-native"] = "tme"
    harness: Literal["deepseek", "claude-code", "codex"]
    provider: Literal["vllm", "openai", "deepseek", "anthropic"] = "vllm"
    base_url: str | None = None
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    api_key_env: KeyEnv | None = None
    serper_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    max_output_tokens: int | None = Field(default=None, gt=0)
    model: str = Field(min_length=1)
    member_reasoning_effort: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]*$")
    planner_mode: Literal["fixed", "generated"] = "fixed"
    planner_model: str | None = None
    planner_chat_template_kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    protocol: Literal["point_to_point"] = "point_to_point"
    seed: int
    max_turns: int = Field(gt=0)
    max_team_tokens: int | None = Field(default=None, gt=0)
    max_agent_tokens: int | None = Field(default=None, gt=0)
    judge_model: str = Field(min_length=1)
    judge_provider: Literal["vllm", "openai", "anthropic"] = "vllm"
    judge_base_url: str | None = None
    judge_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    judge_api_key_env: KeyEnv | None = None

    @model_validator(mode="after")
    def check_run_settings(self):
        if (self.benchmark == "drbench") != (self.planner_mode == "generated"):
            raise ValueError("历史 DRBench 要求 generated，Research 要求 fixed")
        if self.runner == "codex-native" and (self.benchmark != "drbench" or self.harness != "codex"):
            raise ValueError("此运行入口的 codex-native 需要 DRBench 和 codex harness")
        if self.harness == "codex" and self.provider not in {"openai", "vllm"}:
            raise ValueError("Codex 成员需要提供 Responses API 的 openai 或 vllm 端点")
        levels = {
            "claude-code": ("low", "medium", "high", "xhigh", "max"),
            "deepseek": ("off", "low", "high", "max"),
        }.get(self.harness)
        effort = self.member_reasoning_effort
        if levels and effort is not None and effort not in levels:
            raise ValueError(f"{self.harness} 的 member_reasoning_effort 可填 {', '.join(levels)}")
        if self.max_team_tokens is None and self.max_agent_tokens is None:
            raise ValueError("请配置 max_agent_tokens 或 max_team_tokens")
        return self

    @field_validator("provider", "judge_provider", mode="before")
    @classmethod
    def legacy_local(cls, value):
        return "vllm" if value == "local" else value

    @field_validator("base_url", "judge_base_url")
    @classmethod
    def validate_base_url(cls, value):
        return api_base(value) if value is not None else None

    @classmethod
    def for_tasks(cls, data):
        """将实验 YAML 展开为逐题配置，每份轨迹只保存自己的题目。"""
        if not isinstance(data, dict) or "task_ids" not in data:
            return [cls.model_validate(data)]
        common = data.copy()
        numbers = common.pop("task_ids")
        if "task_id" in common:
            raise ValueError("task_id 和 task_ids 只能填写一个")
        if common.get("benchmark") == "drbench":
            if (not isinstance(numbers, list) or not numbers
                    or any(not isinstance(n, str) or not re.fullmatch(r"DR[0-9]{4}", n) for n in numbers)
                    or len(set(numbers)) != len(numbers)):
                raise ValueError("DRBench task_ids 需要无重复的题号列表，例如 [DR0001, DR0002, DR0003]")
            return [cls.model_validate({**common, "task_id": n}) for n in numbers]
        if not isinstance(numbers, list) or not numbers or any(
            type(n) is not int or n < 1 for n in numbers
        ):
            raise ValueError("task_ids 需要非空的正整数列表，例如 [1, 2, 3]")
        if len(set(numbers)) != len(numbers):
            raise ValueError("task_ids 中有重复题号")
        return [
            cls.model_validate({**common, "task_id": f"research/{n}"}) for n in numbers
        ]


async def execute(
    config,
    directory,
    *,
    loader=load,
    gateway_class=Gateway,
    sandbox_class=Sandbox,
    grader=None,
):
    endpoints = {
        "members": endpoint_url(config.provider, config.base_url),
        "planner": endpoint_url(config.provider, config.base_url, chat=True),
        "judge": endpoint_url(
            config.judge_provider, config.judge_base_url, judge=True, chat=True
        ),
    }
    local_endpoint = next(
        (url for url in endpoints.values() if is_validation_endpoint(url)), None
    )
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("members", "submissions"):
        (directory / name).mkdir()
    manifest = {
        "run_id": directory.name,
        "started": datetime.now(UTC).isoformat(),
        "config": config.model_dump(),
        "endpoints": endpoints,
        "purpose": "framework_validation" if local_endpoint else "evaluation",
        "models": {
            "planner": config.planner_model or config.model,
            "members": config.model,
            "judge": config.judge_model,
        },
    }
    if local_endpoint:
        manifest["local_endpoint"] = local_endpoint
    write_json(directory / "run.json", manifest)
    store = Store(directory)
    result = {"status": "infra_error", "error": "启动失败"}
    scores = {"status": "not_run"}
    env = None
    try:
        task = loader(config.benchmark, config.task_id)
        manifest.update(revision=task.revision, role_source=task.role_source)
        write_json(directory / "run.json", manifest)
        gateway = gateway_class(config, store)
        with ExitStack() as resources:
            if task.benchmark == "drbench":
                manifest["drbench_environment"] = resources.enter_context(app_tools(task, config, directory))
                write_json(directory / "run.json", manifest)
            service = resources.enter_context(TeamService(task, store, gateway))
            sandbox = sandbox_class(directory, config, service)
            env = PointToPointEnv(
                store,
                sandbox,
                gateway.complete,
                max_turns=config.max_turns,
                max_team_tokens=config.max_team_tokens,
                max_agent_tokens=config.max_agent_tokens,
            )
            env.start(task, config.seed)
            while env.running:
                await env.step()
            result = env.result()
        with store.transaction() as db:
            files = {}
            for slot, entry in result["submissions"].items():
                row = archive.read(db, entry["actor"], entry["blob"])
                files[slot] = row["data"]
                (directory / "submissions" / slot).write_bytes(row["data"])
        if result["status"] == "completed":
            try:
                if grader is None:
                    values = await asyncio.to_thread(
                        score, task, files, config.judge_model, gateway
                    )
                else:
                    values = grader(task, files, config.judge_model)
                scores = {
                    "status": "scored",
                    "model": config.judge_model,
                    "metrics": values,
                }
            except Exception as error:  # noqa: BLE001 — 上游评分器抛出的异常与团队执行状态分别保存
                scores = {
                    "status": "evaluation_error",
                    "model": config.judge_model,
                    "error": f"{type(error).__name__}: {error}",
                }
    except asyncio.CancelledError:
        result = {"status": "cancelled", "error": "运行收到停止请求"}
        if env is not None:
            env.fail(result["status"], result["error"])
            result = env.result()
    except Exception as error:  # noqa: BLE001 — 为运行入口的第三方异常保留原始错误
        detail = f"{type(error).__name__}: {error}"
        if env is not None:
            env.fail("infra_error", detail)
            result = env.result()
        else:
            result = {"status": "infra_error", "error": detail}
    finally:
        try:
            with store.transaction() as db:
                events.emit(
                    db,
                    "run_finished",
                    status=result["status"],
                    scoring=scores["status"],
                )
                result["usage"] = budget.summary(db)
                events.export(db, directory / "events.jsonl")
            write_json(directory / "result.json", result)
            write_json(directory / "scores.json", scores)
        finally:
            store.close()
    return result, scores


def run_one(config, out):
    if config.runner == "codex-native":
        from experiments.run_codex_research import run_drbench_native

        return run_drbench_native(config, out)

    async def start():
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM, current.cancel)
        try:
            return await execute(config, out)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    result, scores = asyncio.run(start())
    print(
        json.dumps(
            {
                "task_id": config.task_id,
                "directory": str(out),
                "status": result["status"],
                "error": result.get("error"),
                "scoring": scores["status"],
                "scoring_error": scores.get("error"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return int(result["status"] != "completed" or scores["status"] != "scored")


def run_child(config, out):
    # 终端中断由父进程接收并转为每个团队的 SIGTERM，避免重复取消。
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise SystemExit(run_one(config, out))


def run_batch(configs, root):
    """每个进程运行一道题，逐题记录成败。"""
    children = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    ctx = multiprocessing.get_context("spawn")
    batch_id = uuid4().hex
    try:
        for config in configs:
            if stopping:
                break
            out = root / f"{batch_id}-{config.task_id.replace('/', '-')}"
            print(f"{config.task_id} → {out}", flush=True)
            child = ctx.Process(target=run_child, args=(config, out))
            child.start()
            children.append(child)
        for child in children:
            while not stopping and child.is_alive():
                child.join(timeout=0.5)
        return int(stopping or any(child.exitcode != 0 for child in children))
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
        for child in children:
            child.join()
            child.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    for key in (
        "runner",
        "harness",
        "provider",
        "base-url",
        "api-key-env",
        "protocol",
        "model",
        "member-reasoning-effort",
        "planner-mode",
        "planner-model",
        "judge-model",
        "judge-provider",
        "judge-base-url",
        "judge-api-key-env",
    ):
        parser.add_argument("--" + key)
    for key in (
        "max-turns",
        "max-team-tokens",
        "max-agent-tokens",
        "max-output-tokens",
        "seed",
    ):
        parser.add_argument("--" + key, type=int)
    parser.add_argument("--output", type=Path)
    opts = vars(parser.parse_args())
    data = yaml.safe_load(opts.pop("config").read_text())
    out = opts.pop("output")
    if not isinstance(data, dict):
        parser.error("实验 YAML 顶层需要是映射")
    overrides = {k: v for k, v in opts.items() if v is not None}
    try:
        configs = Config.for_tasks({**data, **overrides})
    except ValueError as error:
        parser.error(str(error))
    runs_root = ROOT / "experiments/runs"
    if len(configs) == 1:
        return run_one(configs[0], out or runs_root / uuid4().hex)
    return run_batch(configs, out or runs_root)


if __name__ == "__main__":
    raise SystemExit(main())
