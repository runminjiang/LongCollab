"""用真实成员容器和回环脚本端点核对首次与恢复会话的推理档位。"""

import asyncio
import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from run import Config
from runtime.envs.team import Member
from runtime.sandbox import Sandbox
from service.api import TeamService
from service.gateway import Gateway
from service.store import Store, write_json
from tests.fakes import model_server, sample_task


async def verify(harness, provider, effort):
    received = []

    def respond(body):
        received.append({k: body[k] for k in (
            "reasoning", "reasoning_effort", "thinking", "output_config",
        ) if k in body})
        return '{"kind":"ready"}'

    with TemporaryDirectory(prefix="tme-effort-") as tmp, model_server(respond) as (url, _):
        config = Config(
            benchmark="marble", task_id="research/1", harness=harness, provider=provider,
            model="scripted-model", base_url=url + "/v1", member_reasoning_effort=effort,
            planner_mode="fixed", protocol="point_to_point", seed=7, max_turns=40,
            max_agent_tokens=500000, judge_model="fixture",
        )
        with closing(Store(tmp)) as store, TeamService(sample_task(), store, Gateway(config, store)) as service:
            sandbox = Sandbox(tmp, config, service, timeout=75)
            member = Member("A", "A", "probe", [])
            sandbox.prepare(member)
            for phase in ("start", "resume"):
                reply = await sandbox.turn(member, {"step": phase}, phase)
                assert reply["action"] == {"kind": "ready"}
                if phase == "resume":
                    assert member.session_id == reply["session_id"]
                member.session_id = reply["session_id"]
            expected = "none" if provider == "openai" and effort == "off" else effort
            assert len(received) >= 2
            for body in received:
                if harness == "codex":
                    assert body["reasoning"]["effort"] == expected
                elif provider == "openai":
                    assert body["reasoning_effort"] == expected
                elif effort == "off":
                    assert body["thinking"]["type"] == "disabled" and "output_config" not in body
                else:
                    assert body["output_config"]["effort"] == expected
            with store.transaction() as db:
                rows = db.execute("SELECT data FROM events WHERE kind='model_request' ORDER BY seq")
                assert [json.loads(row[0])["member_reasoning_effort"] for row in rows] == [expected] * len(received)
            return dict(harness=harness, provider=provider, configured=effort,
                        image_id=sandbox.image_id, requests=received)


async def main():
    cases = [
        ("codex", "openai", "high"), ("claude-code", "openai", "high"),
        ("deepseek", "openai", "high"), ("claude-code", "anthropic", "high"),
        ("deepseek", "deepseek", "high"), ("deepseek", "deepseek", "off"),
        ("deepseek", "openai", "off"),
    ]
    results = [await verify(*case) for case in cases]
    write_json(Path(__file__).with_name("results") / "member-effort.json", results)


if __name__ == "__main__":
    asyncio.run(main())
