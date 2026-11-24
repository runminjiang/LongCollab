import json

import pytest

from service import events
from tests.test_team import finish, team
from tools.visualization.history import native_history
from tools.visualization.server import run_list, snapshot


async def test_live_inputs_match_sandbox_and_archive(tmp_path):
    root = tmp_path / "run"

    def reply(service, token, prompt):
        service.request(token, "/team", {"op": "list"})
        return {"kind": "handoff", "to": "A", "text": "反馈", "ready": True}

    with team(
        root,
        {"A": [{"kind": "handoff", "to": "B", "text": "初稿"}, finish], "B": [reply]},
    ) as (env, sandbox):
        assert run_list(tmp_path) == [root]
        original = sandbox.turn
        observed = []

        async def check_live(member, prompt, turn):
            trace = snapshot(root, 0)
            start = trace["events"][-1]
            assert start["kind"] == "activated" and start["turn_id"] == turn
            assert start["input"] == prompt
            observed.append(start)
            return await original(member, prompt, turn)

        sandbox.turn = check_live
        while env.running:
            await env.step()
        assert env.result()["status"] == "completed"
        assert observed[0]["input"]["task"]["goal"] == "Write the idea."
        assert observed[1]["input"]["mail"][0]["message"] == "初稿"
        assert observed[2]["input"]["mail"][0]["message"] == "反馈"
        all_events = snapshot(root, 0)["events"]
        tool = next(e for e in all_events if e["kind"] == "environment_tool")
        assert tool["result"]["files"] == []
        cursor = observed[1]["seq"]
        assert snapshot(root, cursor)["events"] == [
            e for e in all_events if e["seq"] > cursor
        ]

        archive = tmp_path / "archive"
        archive.mkdir()
        with env.store.transaction() as db:
            events.export(db, archive / "events.jsonl")
        assert snapshot(archive, 0)["events"] == all_events
        assert not snapshot(archive, 0)["has_db"]


def test_old_run_and_partial_native_log(tmp_path):
    path = tmp_path / "members/A/home/claude/projects/-work"
    path.mkdir(parents=True)
    log = path / "s.jsonl"
    row = {
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private"},
                {"type": "text", "text": "公开回复"},
            ],
        },
    }
    line = json.dumps(row).encode() + b"\n"
    log.write_bytes(line + line[:8])
    h = native_history(tmp_path, "A", "")
    assert len(h["records"]) == 1
    assert h["records"][0]["content"] == [{"type": "text", "text": "公开回复"}]
    assert native_history(tmp_path, "A", h["version"]) == {"version": h["version"]}
    with log.open("ab") as stream:
        stream.write(line[8:])
    assert len(native_history(tmp_path, "A", h["version"])["records"]) == 2
    (path / "linked.jsonl").symlink_to(log)
    assert len(native_history(tmp_path, "A", "")["records"]) == 2
    with pytest.raises(ValueError):
        native_history(tmp_path, "..", "")

    old = {"seq": 1, "kind": "activated", "actor": "A", "messages": []}
    (tmp_path / "events.jsonl").write_text(json.dumps(old) + "\n")
    assert snapshot(tmp_path, 0)["events"] == [old]
    assert not (tmp_path / "state.sqlite").exists()
