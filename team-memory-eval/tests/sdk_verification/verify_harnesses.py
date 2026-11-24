"""真实镜像与原生会话，供应商 HTTP 用脚本端点替代。"""

import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from runtime.envs.team import Member
from runtime.sandbox import Sandbox
from service.api import TeamService
from service.gateway import Gateway
from service.store import Store, write_json
from tests.fakes import model_server, sample_task


class Probe:
    def __init__(self, marble=False):
        self.marble = marble
        self.primary = uuid4().hex
        self.histories = {}
        self.tools = {}

    def __call__(self, data):
        msgs = data["messages"] if "messages" in data else data["input"]
        for idx in range(len(msgs) - 1, -1, -1):
            if msgs[idx].get("role") != "user":
                continue
            content = msgs[idx]["content"]
            text = (
                content
                if isinstance(content, str)
                else "".join(b.get("text", "") for b in content if isinstance(b, dict))
            )
            match = re.search(
                r'"probe"\s*:\s*"(seed|resume|neighbor|check)"', text
            )
            if match:
                break
        else:
            return json.dumps({"kind": "ready"})
        stage = match.group(1)
        history = json.dumps(msgs, ensure_ascii=False)
        self.histories[stage] = self.primary in history
        ops = [f.get("function", f) for f in data.get("tools", [])]
        self.tools[stage] = [o["name"] for o in ops]
        if stage in {"seed", "neighbor"} and not any(
            m.get("role") == "tool"
            or m.get("type") == "function_call_output"
            or isinstance(m.get("content"), list)
            and any(
                b.get("type") == "tool_result"
                for b in m["content"]
                if isinstance(b, dict)
            )
            for m in msgs[idx + 1 :]
        ):
            op = next((o for o in ops if o["name"].lower() in {"bash", "exec_command"}), None)
            if not op:
                raise RuntimeError("无 Bash 工具: " + str(self.tools[stage]))
            commands = {
                "seed": "printf main > evidence.txt && python -m tools.client upload evidence.txt > upload.json",
                "neighbor": "if test -f evidence.txt; then printf leak > inspection; else printf isolated > inspection; fi",
            }
            if self.marble:
                commands["seed"] += (
                    " && python -m tools.client list > catalog.json"
                    " && ls /sys/class/net > interfaces"
                    # TEST-NET-1 的数字地址用来检查路由，独立于 DNS 和目标服务器。
                    " && python -c 'import errno, socket; s=socket.socket(); s.settimeout(2); "
                    'print(errno.errorcode.get(s.connect_ex(("192.0.2.1", 443))))\' > egress'
                    """ && python -m tools.client call fetch_webpage '{"url":"http://127.0.0.1/"}' > fetch.json"""
                )
            schema = op.get("input_schema") or op.get("parameters") or {}
            props = schema.get("properties", {})
            key = next(
                (k for k in ("command", "script", "cmd") if k in props), "command"
            )
            args = {key: commands[stage]}
            if "description" in schema.get("required", []):
                args["description"] = "verify private workspace"
            return op["name"], args
        return json.dumps({"kind": "ready"})


async def verify(harness, provider="anthropic", *, task=None):
    probe = Probe(marble=task is not None)
    protocol = "responses" if harness == "codex" else provider
    config = SimpleNamespace(
        model="scripted-model",
        benchmark="marble" if task else "fixture",
        planner_model=None,
        harness=harness,
        max_team_tokens=200000,
        provider="openai" if harness == "codex" else provider,
        api_key_env=None,
        max_output_tokens=None,
    )
    with (
        tempfile.TemporaryDirectory(prefix="tme-verify-") as tmp,
        model_server(probe, protocol=protocol) as (url, calls),
    ):
        config.base_url = url
        store = Store(tmp)
        try:
            with TeamService(task or sample_task(), store, Gateway(config, store)) as service:
                sandbox = Sandbox(tmp, config, service, timeout=75)
                a = Member("A", "A", "probe", ["research"] if task else [])
                b = Member("B", "B", "probe", [])
                sandbox.prepare(a)
                sandbox.prepare(b)

                async def ask(member, stage, **extra):
                    value = await sandbox.turn(
                        member, {"probe": stage, **extra}, uuid4().hex
                    )
                    if value["action"] != {"kind": "ready"}:
                        raise AssertionError(f"回合动作异常 {value}")
                    member.session_id = value["session_id"]
                    member.started = True
                    return member.session_id

                main_id = await ask(a, "seed", marker=probe.primary)
                if task:
                    from scenarios.marble import ACTIONS

                    work = sandbox.path(a) / "work"
                    assert json.loads((work / "catalog.json").read_text())["operations"] == list(ACTIONS)
                    assert (work / "interfaces").read_text().split() == ["lo"]
                    egress = (work / "egress").read_text().strip()
                    assert egress == "ENETUNREACH", egress
                    assert "公网地址" in json.loads((work / "fetch.json").read_text())["error-msg"]
                assert (sandbox.path(a) / "work/evidence.txt").read_text() == "main"
                uploaded = json.loads(
                    (sandbox.path(a) / "work/upload.json").read_text()
                )["id"]
                from service.archive import read

                with store.transaction() as db:
                    assert read(db, "A", uploaded)["data"] == b"main"
                assert await ask(a, "resume") == main_id
                await ask(b, "neighbor")
                assert (sandbox.path(b) / "work/inspection").read_text() == "isolated"
                assert await ask(a, "check") == main_id
                assert (sandbox.path(a) / "work/evidence.txt").read_text() == "main"
                assert probe.histories["resume"] and probe.histories["check"]
                assert not probe.histories["neighbor"]
                assert b.session_id != main_id
                available = sorted(
                    {name for names in probe.tools.values() for name in names}
                )
                delegation = {
                    "agent",
                    "task",
                    "spawn_agent",
                    "subagent",
                    "delegate",
                    "teamcreate",
                    "sendmessage",
                }
                assert not {name.lower() for name in available} & delegation
                if task:
                    native_names = {name.lower().replace("_", "") for name in available}
                    assert native_names.isdisjoint({"websearch", "webfetch"}), available
                return {
                    "status": "passed",
                    "provider": "scripted HTTP",
                    "protocol": protocol,
                    **(
                        {"environment_tools": list(task.operations), "interfaces": ["lo"], "direct_connect": egress}
                        if task else {}
                    ),
                    "image": sandbox.image_tag,
                    "image_id": sandbox.image_id,
                    "calls": len(calls),
                    "tools": available,
                    "verified": [
                        "resume",
                        "workspace_isolation",
                        "history_isolation",
                        "file_upload",
                    ],
                    "histories": probe.histories,
                    "session_id": main_id,
                    "neighbor_id": b.session_id,
                }
        except Exception as error:  # noqa: BLE001 — 原样保存探针错误并继续验证另一种 SDK
            return {
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
                "calls": len(calls),
                "histories": probe.histories,
                "tools": probe.tools,
            }
        finally:
            store.close()


async def verify_vllm(harness):
    """按实验 YAML 在真实容器中执行一个无需工具的回合。"""
    import yaml
    from run import Config
    from scenarios import ROOT

    raw = yaml.safe_load((ROOT / "experiments/drbench.yaml").read_text())
    config = Config.model_validate({**raw, "harness": harness})
    if config.provider != "vllm":
        raise ValueError("该探针需要 vLLM 服务")
    with tempfile.TemporaryDirectory(prefix="tme-vllm-") as tmp:
        store = Store(tmp)
        try:
            with TeamService(sample_task(), store, Gateway(config, store)) as service:
                sandbox = Sandbox(tmp, config, service, timeout=90)
                member = Member("A", "probe", "answer", [])
                sandbox.prepare(member)
                reply = await sandbox.turn(
                    member,
                    {
                        "instructions": 'Reply directly with exactly {"kind":"ready"}. Do not call tools.'
                    },
                    uuid4().hex,
                )
            with store.transaction() as db:
                calls = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT data FROM events WHERE kind='model_request' ORDER BY seq"
                    )
                ]
            assert reply["action"] == {"kind": "ready"} and reply["session_id"]
            assert calls and all(
                c["model"] == config.model and c["http_status"] == 200 for c in calls
            )
            return {
                "status": "passed",
                "provider": config.provider,
                "model": config.model,
                "base_url": config.base_url,
                "action": reply["action"],
                "calls": len(calls),
                "tokens": sum(c["tokens"] for c in calls),
                "session_id": reply["session_id"],
            }
        except Exception as error:
            return {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        finally:
            store.close()


async def local_benchmarks(output, harness=None):
    import yaml

    from run import Config, execute
    from scenarios import ROOT

    cases = [
        (b, h, "point_to_point")
        for b in ("drbench", "marble")
        for h in ([harness] if harness else ("deepseek", "claude-code"))
    ]
    rows = []
    for benchmark, driver, protocol in cases:
        if harness and driver != harness:
            continue
        raw = yaml.safe_load((ROOT / "experiments" / (benchmark + ".yaml")).read_text())
        config = Config.model_validate(
            {
                **raw,
                "harness": driver,
                "protocol": protocol,
            }
        )
        if (config.provider, config.judge_provider) != ("vllm", "vllm"):
            raise ValueError("本地验收要求 planner、成员及评委均使用 vLLM")
        directory = (
            ROOT
            / "experiments/runs"
            / f"local-{benchmark}-{driver}-{protocol}-{uuid4().hex[:8]}"
        )
        result, scores = await execute(config, directory)
        trace = [
            json.loads(line)
            for line in (directory / "events.jsonl").read_text().splitlines()
        ]
        calls = [e for e in trace if e["kind"] in {"model_request", "judge_request"}]
        if any(
            e.get("model")
            != (config.judge_model if e["kind"] == "judge_request" else config.model)
            or e.get("provider") != "vllm"
            for e in calls
        ):
            raise AssertionError("本地验证出现了其他模型路由")
        rows.append(
            {
                "run": str(directory.relative_to(ROOT)),
                "benchmark": benchmark,
                "harness": driver,
                "protocol": protocol,
                "seed": config.seed,
                "max_team_tokens": config.max_team_tokens,
                "model": config.model,
                "members": {
                    key: m["assignment"]
                    for key, m in result.get("members", {}).items()
                    if m["category"] == "worker"
                },
                "turns": result.get("turns", 0),
                "status": result["status"],
                "error": result.get("error", ""),
                "submissions": list(result.get("submissions", {})),
                "local_requests": len(calls),
                "activations": [
                    [e["actor"], e["reason"]] for e in trace if e["kind"] == "activated"
                ],
                "scores": scores,
            }
        )
        write_json(output, {"purpose": "framework_validation", "runs": rows})
        print(
            benchmark, driver, protocol, result["status"], scores["status"], flush=True
        )
    return int(
        any(
            r["status"] != "completed" or r["scores"]["status"] != "scored"
            for r in rows
        )
    )


def grader_fixtures():
    report = {
        "report_text": "Pilot opt-in personalized offers and measure incremental sales against a control.",
        "report_insights": [
            {
                "claim": "Test opt-in offers with a randomized control group.",
                "citations": [],
            }
        ],
    }
    return [
        (
            "marble",
            "research/1",
            {
                "idea": b"Evaluate a personalized federated learning method that reweights local updates by held-out client accuracy. Compare with FedAvg under non-IID partitions."
            },
        ),
        ("drbench", "DR0002", {"report.json": json.dumps(report).encode()}),
    ]


def fixture_responder(task, slot, content):
    import shlex

    from tests.fakes import fixed

    planned = fixed(task)
    first, second = [r["id"] for r in planned["members"][:2]]

    def respond(data):
        messages = data.get("messages", data.get("input", []))
        for i in reversed(range(len(messages))):
            msg = messages[i]
            if msg.get("role") != "user":
                continue
            value = msg["content"]
            text = (
                value
                if isinstance(value, str)
                else "\n".join(b.get("text", "") for b in value)
            )
            if text.startswith("Make one team plan."):
                return json.dumps(planned)
            match = re.search(r'\{\s*"(?:task|member)"\s*:', text)
            if match:
                prompt = json.JSONDecoder().raw_decode(text[match.start() :])[0]
                if "wake_reason" in prompt:
                    break
        else:
            return json.dumps({"kind": "ready"})
        actor = prompt["member"]
        if actor == first and prompt["wake_reason"] == "planner_first":
            return json.dumps(
                {
                    "kind": "handoff",
                    "to": second,
                    "message": "Check the sample before delivery.",
                }
            )
        if actor == second:
            return json.dumps(
                {
                    "kind": "handoff",
                    "to": first,
                    "message": "Sample was checked.",
                    "ready": True,
                }
            )
        if actor == first:
            for turn in messages[i + 1 :]:
                blocks = turn.get("content")
                if turn.get("type") == "function_call_output":
                    blocks = [{"type": "tool_result", "content": turn["output"]}]
                if not isinstance(blocks, list):
                    continue
                for part in blocks:
                    if part.get("type") == "tool_result":
                        content_blocks = part["content"]
                        output = (
                            content_blocks
                            if isinstance(content_blocks, str)
                            else "\n".join(
                                b["text"]
                                for b in content_blocks
                                if b.get("type") == "text"
                            )
                        )

                        receipt = re.search(
                            r'"id"\s*:\s*"([a-f0-9]{32})"',
                            output,
                        )
                        if receipt:
                            return json.dumps(
                                {
                                    "kind": "ready",
                                    "submissions": {slot: receipt.group(1)},
                                }
                            )
                        raise ValueError("原生上传未返回附件 ID")
            tool = next(t for t in data["tools"] if t["name"].lower() in {"bash", "exec_command"})
            code = f"from pathlib import Path; Path('sample').write_bytes({content!r})"
            command = (
                "python -c "
                + shlex.quote(code)
                + " && python -m tools.client upload sample"
            )
            args = {"cmd" if tool["name"] == "exec_command" else "command": command}
            if "description" in tool.get("input_schema", {}).get("required", []):
                args["description"] = "Upload the test fixture"
            return tool["name"], args
        return json.dumps({"kind": "ready"})

    return respond


async def scripted_benchmarks(output, selected):
    import yaml

    from run import Config, execute
    from scenarios import ROOT, load

    rows = []
    for bench, task_id, files in grader_fixtures():
        task = load(bench, task_id)
        slot, content = next(iter(files.items()))

        respond = fixture_responder(task, slot, content)
        for harness in [selected] if selected else ["deepseek", "claude-code"]:
            raw = yaml.safe_load((ROOT / "experiments" / (bench + ".yaml")).read_text())
            out = (
                ROOT
                / "experiments/runs"
                / f"fixture-{bench}-{harness}-{uuid4().hex[:8]}"
            )
            with model_server(respond) as (url, _):
                config = Config.model_validate(
                    {
                        **raw,
                        "harness": harness,
                        "model": "scripted-model",
                        "provider": "openai" if harness == "codex" else "anthropic",
                        "base_url": url,
                        "api_key_env": None,
                    }
                )
                result, scores = await execute(config, out)
            rows.append(
                {
                    "benchmark": bench,
                    "harness": harness,
                    "run": str(out.relative_to(ROOT)),
                    "status": result["status"],
                    "error": result.get("error"),
                    "scores": scores,
                }
            )
            write_json(
                output,
                {
                    "purpose": "native_end_to_end_verification",
                    "worker_model": "scripted HTTP",
                    "artifact_source": "handwritten_fixture",
                    "judge": config.judge_provider + "/" + config.judge_model,
                    "runs": rows,
                },
            )
            print(bench, harness, result["status"], scores["status"], flush=True)
    return int(
        any(
            r["status"] != "completed" or r["scores"]["status"] != "scored"
            for r in rows
        )
    )


def local_graders(output):
    import yaml

    from eval.score import score
    from run import Config
    from scenarios import ROOT, load

    results = {}
    for benchmark, task_id, files in grader_fixtures():
        with tempfile.TemporaryDirectory(prefix="tme-grader-") as tmp:
            store = Store(tmp)
            config = Config.model_validate(
                yaml.safe_load(
                    (ROOT / "experiments" / (benchmark + ".yaml")).read_text()
                )
            )
            if config.judge_provider != "vllm":
                raise ValueError("本地评分验证需要 vLLM 评委")
            gateway = Gateway(config, store)
            try:
                task = load(benchmark, task_id)
                metrics = score(task, files, config.judge_model, gateway)
                result = {"status": "scored", "metrics": metrics}
            except Exception as error:  # noqa: BLE001 — 保存上游评分器的原始异常
                result = {
                    "status": "evaluation_error",
                    "error": f"{type(error).__name__}: {error}",
                }
            with store.transaction() as db:
                result["judge_requests"] = db.execute(
                    "SELECT count(*) FROM events WHERE kind='judge_request'"
                ).fetchone()[0]
            store.close()
        results[benchmark] = {
            **result,
            "model": config.judge_model,
            "artifacts": {k: v.decode() for k, v in files.items()},
        }
        write_json(
            output,
            {
                "purpose": "scorer_interface_verification",
                "artifact_source": "handwritten_fixture",
                "results": results,
            },
        )
        print(
            benchmark,
            result["status"],
            "judge requests",
            result["judge_requests"],
            flush=True,
        )
    return int(any(r["status"] != "scored" for r in results.values()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=["deepseek", "claude-code", "codex"])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--marble-tools", action="store_true", help="用脚本端点核对三种真实容器的 MARBLE 工具与隔离")
    mode.add_argument(
        "--vllm-smoke", action="store_true", help="用实际 vLLM 运行无需工具的原生回合"
    )
    mode.add_argument(
        "--openai-tools",
        action="store_true",
        help="用 Chat Completions 脚本端点验证原生工具和会话",
    )
    mode.add_argument(
        "--local-benchmarks",
        action="store_true",
        help="用本机 Qwen 验证原题的逐人交棒",
    )
    mode.add_argument(
        "--local-graders",
        action="store_true",
        help="用给定样稿单独验证两套官方评分器与本地评委",
    )
    mode.add_argument(
        "--scripted-benchmarks",
        action="store_true",
        help="原题、原生容器、脚本化成员和本地官方评委的全链路验证",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).parent / "results"
    if args.scripted_benchmarks:
        return asyncio.run(
            scripted_benchmarks(
                args.output or root / "scripted-benchmarks.json", args.harness
            )
        )
    if args.local_graders:
        return local_graders(args.output or root / "local-graders.json")
    if args.local_benchmarks:
        return asyncio.run(
            local_benchmarks(
                args.output or root / "local-benchmarks.json", args.harness
            )
        )
    if args.marble_tools:
        from functools import partial
        from scenarios import load

        name, probe = "marble-harnesses", partial(verify, task=load("marble", "research/2"))
    elif args.vllm_smoke:
        name, probe = "vllm-harnesses", verify_vllm
    elif args.openai_tools:
        from functools import partial

        name, probe = "openai-harnesses", partial(verify, provider="openai")
    else:
        name, probe = "harnesses", verify
    args.output = args.output or root / (name + ".json")
    results = {}
    defaults = ["deepseek", "claude-code", "codex"] if args.marble_tools else ["deepseek", "claude-code"]
    for h in [args.harness] if args.harness else defaults:
        results[h] = asyncio.run(probe(h))
        write_json(args.output, results)
        print(h, results[h]["status"], results[h].get("error", ""), flush=True)
    return int(any(r["status"] != "passed" for r in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
