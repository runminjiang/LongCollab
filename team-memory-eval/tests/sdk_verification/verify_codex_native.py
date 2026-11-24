"""用回环模型验证原生子代理实际创建、答复，以及单代理对照的工具限制。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from experiments.run_codex_research import codex_image, run_task
from scenarios import load
from service.store import write_json
from tests.fakes import model_server, offers_delegation


def verify(task, image, directory, mode):
    assignment = "Return the worker fixture value."
    response = "worker fixture reply"
    issued = False

    def respond(body):
        nonlocal issued
        if mode == "single":
            assert not offers_delegation(body)
            return "single fixture answer"
        if not issued:
            assert offers_delegation(body)
            issued = True
            return "multi_agent_v1.spawn_agent", {"message": assignment, "fork_context": False}
        actions = [i for i in body["input"] if i.get("type") == "function_call"]
        if not actions:
            return response
        if len(actions) == 1:
            receipt = next(i["output"] for i in body["input"] if i.get("type") == "function_call_output")
            return "multi_agent_v1.wait_agent", {
                "targets": [json.loads(receipt)["agent_id"]], "timeout_ms": 10000,
            }
        return "native fixture answer"

    with model_server(respond, protocol="responses") as (url, _):
        args = SimpleNamespace(endpoint=url + "/v1", model="fixture", effort="low",
                               mode=mode, timeout=75)
        # 这个探针只核对委派行为，场景工具在独立的 MARBLE 探针中验证。
        run_task(task, directory, args, image, [], lambda: {})
    manifest = json.loads((directory / "run.json").read_text())
    assert manifest["status"] == "completed"
    assert (directory / "idea").read_text().strip() == f"{mode} fixture answer"
    team = manifest.get("native_team", [])
    if mode == "native":
        assert len(team) == 1 and team[0]["parent_thread_id"] == manifest["session_id"]
        child = team[0]["thread_id"]
        assert manifest["spawned_subagents"] == [child]
        logs = list((directory / "native-sessions").rglob(f"rollout-*-{child}.jsonl"))
        assert len(logs) == 1
        payloads = [json.loads(line)["payload"] for line in logs[0].read_text().splitlines()]
        assert any(assignment in str(p) for p in payloads)
        assert any(
            p.get("type") == "message" and p.get("role") == "assistant"
            and {"type": "output_text", "text": response} in p["content"]
            for p in payloads
        )
    else:
        assert not team
    return {"status": "passed", "subagents": len(team)}


if __name__ == "__main__":
    task = load("marble", "research/1")
    image = codex_image()
    with TemporaryDirectory(prefix="tme-native-verify-") as tmp:
        results = {mode: verify(task, image, Path(tmp) / mode, mode)
                   for mode in ("native", "single")}
    write_json(Path(__file__).with_name("results") / "codex-native.json", results)
