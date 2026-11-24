"""用脚本端点和评分器验证 Codex 在主运行器中交棒、提交与记账。"""

import asyncio
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from run import Config, execute
from scenarios import load
from service.store import write_json
from tests.fakes import fixed, model_server, offers_delegation


async def verify():
    task = load("marble", "research/2")
    members = [role["id"] for role in task.roles]

    def respond(data):
        if "messages" in data:
            return json.dumps(fixed(task))
        assert not offers_delegation(data), "P2P 成员不能委派原生子代理"
        # 工作成员收到的最后一条用户消息包含本次 P2P 回合的输入。
        messages = data["input"]
        prompt = next(m for m in reversed(messages) if m.get("role") == "user")
        member = json.loads(prompt["content"][0]["text"])["member"]
        index = members.index(member)
        if index + 1 < len(members):
            return json.dumps({
                "kind": "handoff", "to": members[index + 1],
                "message": f"from {member}", "ready": True,
            })
        output = next((m["output"] for m in messages if m.get("type") == "function_call_output"), None)
        if output is None:
            return "exec_command", {"cmd": "echo tested > idea.txt && python -m tools.client upload idea.txt"}
        receipt = re.search(r'"id":\s*"([a-f0-9]{32})"', output)
        assert receipt, "上传应返回附件 ID"
        return json.dumps({"kind": "ready", "submissions": {"idea": receipt[1]}})

    def grade(task, files, model):
        assert files["idea"].strip() == b"tested"
        return {"scripted": True}

    with TemporaryDirectory(prefix="tme-codex-team-") as tmp, model_server(respond) as (url, _):
        config = Config(
            benchmark="marble", task_id=task.id, harness="codex", provider="openai",
            base_url=url + "/v1", model="fixture", planner_mode="fixed", protocol="point_to_point",
            seed=7, max_turns=40, max_agent_tokens=500000, judge_model="fixture",
        )
        root = Path(tmp) / "run"
        result, scores = await execute(config, root, grader=grade)
        assert result["status"] == "completed", result
        assert scores["metrics"] == {"scripted": True}, scores
        events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
        senders = [e["actor"] for e in events if e["kind"] == "committed"]
        assert senders == members and result["turns"] == 5
        charged = {u["actor"]: u["tokens"] - u["cached_tokens"] for u in result["usage"] if u["category"] == "worker"}
        assert [charged[m] for m in members] == [11, 11, 11, 11, 22]
        return {"status": result["status"], "scoring": scores, "turns": result["turns"],
                "senders": senders, "usage": result["usage"]}


if __name__ == "__main__":
    write_json(Path(__file__).with_name("results") / "codex-marble-team.json", asyncio.run(verify()))
