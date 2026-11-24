import json
from contextlib import contextmanager

from runtime.envs.point_to_point import PointToPointEnv
from runtime.envs.team import Action
from service.api import TeamService
from service.store import Store
from tests.fakes import ScriptedSandbox, fixed, sample_task, submit


@contextmanager
def team(path, scripts, n=2, **limits):
    task = sample_task(n)
    store = Store(path)
    service = TeamService(task, store, None)
    sandbox = ScriptedSandbox(path, None, service, scripts)
    env = PointToPointEnv(store, sandbox, lambda _: json.dumps(fixed(task)), **limits)
    try:
        env.start(task, 7)
        yield env, sandbox
    finally:
        store.close()


async def drive(env):
    while env.running:
        await env.step()
    return env.result()


def finish(service, token, prompt):
    return {"kind": "ready", "submissions": submit(service, token)}


def test_only_point_to_point_is_available():
    from run import Config

    assert Config.model_json_schema()["properties"]["protocol"]["const"] == "point_to_point"
    allowed = {"handoff", "ready"}
    assert set(PointToPointEnv.actions) == allowed
    assert set(Action.model_json_schema()["discriminator"]["mapping"]) == allowed


async def test_handoff_and_resume(tmp_path):
    scripts = {
        "A": [
            {"kind": "handoff", "to": "B", "message": "check", "ready": True},
            finish,
        ],
        "B": [{"kind": "handoff", "to": "A", "text": "yes", "ready": True}],
    }
    with team(tmp_path, scripts) as (env, s):
        result = await drive(env)
        assert result["status"] == "completed"
        assert [a for a, _, _ in s.seen] == ["A", "B", "A"]
        assert s.seen[2][2] == s.sessions["A"]
        assert s.seen[1][1]["mail"][0]["message"] == "check"
        assert s.seen[2][1]["mail"][0]["message"] == "yes"
        with env.store.transaction() as db:
            turn = db.execute("SELECT id,actor,result FROM turns LIMIT 1").fetchone()
        env.commit(turn["id"], turn["actor"], json.loads(turn["result"]), [])
        with env.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 3
            assert db.execute("SELECT COUNT(*) FROM mail").fetchone()[0] == 2
            commits = [
                json.loads(row[0])["action"]
                for row in db.execute(
                    "SELECT data FROM events WHERE kind='committed' ORDER BY seq"
                )
            ]
            assert [a["message"] for a in commits if a["kind"] == "handoff"] == [
                "check",
                "yes",
            ]
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='action_rejected'"
                ).fetchone()[0]
                == 0
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='planner_called'"
                ).fetchone()[0]
                == 1
            )


async def test_handoff_correction_reuses_session_and_uploaded_file(tmp_path):
    from service import archive, budget

    action = {"kind": "handoff", "to": "B", "body": "请整合附件", "ready": True}

    def first(service, token, prompt):
        action["files"] = [submit(service, token)["idea"]]
        return action

    def correct(service, token, prompt):
        assert "body" in prompt["error"]
        handoff_schema = prompt["schema"]["discriminator"]["mapping"]["handoff"].split(
            "/"
        )[-1]
        assert "message" in prompt["schema"]["$defs"][handoff_schema]["required"]
        assert "handoff" in prompt["actions"]
        with service.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM mail").fetchone()[0] == 0
            assert db.execute("SELECT actor FROM grants").fetchone()[0] == "A"
        return {k: v for k, v in action.items() if k != "body"} | {
            "message": action["body"]
        }

    with team(tmp_path, {"A": [first, correct], "B": [finish]}, max_turns=2) as (
        env,
        sandbox,
    ):
        result = await drive(env)
        assert result["status"] == "completed" and result["turns"] == 2
        assert [m for m, _, _ in sandbox.seen] == ["A", "A", "B"]
        assert sandbox.seen[1][2] == sandbox.sessions["A"]
        [mail] = sandbox.seen[2][1]["mail"]
        assert (mail["from"], mail["message"], mail["files"]) == (
            "A",
            action["body"],
            action["files"],
        )
        with env.store.transaction() as db:
            assert archive.read(db, "B", mail["files"][0])["data"] == b"result"
            assert budget.agent_used(db, "A") == 6
            trace = [
                (k, json.loads(data))
                for k, data in db.execute(
                    "SELECT kind,data FROM events WHERE kind IN ('activated','action_rejected','committed') ORDER BY seq"
                )
            ]
        first_turn = [(k, e) for k, e in trace if e["actor"] == "A"]
        assert [k for k, _ in first_turn] == [
            "activated",
            "action_rejected",
            "committed",
        ]
        assert len({e["turn_id"] for _, e in first_turn}) == 1
        assert first_turn[1][1]["correction_input"] == sandbox.seen[1][1]


async def test_initial_notifications_and_stall(tmp_path):
    with team(tmp_path, {k: [{"kind": "ready"}] for k in "ABC"}, n=3) as (env, s):
        result = await drive(env)
        assert result["status"] == "stalled" and "idea" in result["error"]
        assert sorted(a for a, _, _ in s.seen) == list("ABC")
        assert s.seen[1][1]["wake_reason"] == "initial_assignment"


async def test_failed_delivery_rolls_back_before_correction(tmp_path):
    from service import archive, messages

    def reject(service, token, prompt):
        output = submit(service, token)
        return {
            "kind": "handoff",
            "to": "A",
            "text": "review",
            "submissions": output,
            "files": [output["idea"], "no-access"],
        }

    def correct(service, token, prompt):
        assert "未获授权" in prompt["error"]
        with service.store.transaction() as db:
            assert not archive.latest(db)
            assert not messages.inbox(db, "A")
            assert len(messages.inbox(db, "B")) == 1
            assert (
                db.execute("SELECT COUNT(*) FROM grants WHERE actor='A'").fetchone()[0]
                == 0
            )
            file_id = db.execute("SELECT blob FROM grants WHERE actor='B'").fetchone()[
                0
            ]
        return {
            "kind": "handoff",
            "to": "A",
            "text": "review",
            "ready": True,
            "files": [file_id],
            "submissions": {"idea": file_id},
        }

    scripts = {
        "A": [{"kind": "handoff", "to": "B", "text": "prepare"}, {"kind": "ready"}],
        "B": [reject, correct],
    }
    with team(tmp_path, scripts) as (env, _):
        result = await drive(env)
        assert result["status"] == "completed" and result["turns"] == 3
        assert result["submissions"]["idea"]["version"] == 1
        with env.store.transaction() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM mail WHERE sender='B'").fetchone()[0]
                == 1
            )


async def test_exhausted_budget_blocks_action_correction(tmp_path):
    invalid = {"kind": "handoff", "to": "B", "body": "over budget"}
    for limit in ("max_agent_tokens", "max_team_tokens"):
        with team(tmp_path / limit, {"A": [invalid], "B": [finish]}, **{limit: 3}) as (
            env,
            sandbox,
        ):
            result = await drive(env)
            assert result["status"] == "budget_exhausted"
            actors = [actor for actor, _, _ in sandbox.seen]
            assert actors == (["A", "B"] if limit == "max_agent_tokens" else ["A"])
            with env.store.transaction() as db:
                rejection = db.execute(
                    "SELECT data FROM events WHERE kind='action_rejected'"
                ).fetchone()
                assert json.loads(rejection[0])["correction_input"] is None


async def test_budget_and_invalid_action(tmp_path):
    with team(
        tmp_path / "budget",
        {"A": [{"kind": "handoff", "to": "B", "text": "x"}]},
        max_turns=1,
    ) as (env, _):
        result = await drive(env)
        assert (
            result["status"] == "budget_exhausted" and "1 个工作回合" in result["error"]
        )
    invalid = {"kind": "handoff", "to": "unknown", "text": "x"}
    with team(tmp_path / "invalid", {"A": [invalid, invalid]}) as (env, sandbox):
        assert (await drive(env))["status"] == "invalid_action"
        assert len(sandbox.seen) == 2 and env.result()["turns"] == 1
        assert "目标工作成员不存在" in sandbox.seen[1][1]["error"]
        with env.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM mail").fetchone()[0] == 0
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='action_rejected'"
                ).fetchone()[0]
                == 2
            )


async def test_parallel_step_requests_are_serial(tmp_path):
    import asyncio

    with team(tmp_path, {"A": [{"kind": "ready"}], "B": [finish]}) as (env, s):
        real_turn = s.turn
        peak = active = 0

        async def measured(*args):
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return await real_turn(*args)
            finally:
                active -= 1

        s.turn = measured
        await asyncio.gather(env.step(), env.step())
        assert peak == 1
        assert [a for a, _, _ in s.seen] == ["A", "B"]
        assert env.result()["status"] == "completed"


async def test_exhausted_worker_returns_handoff_and_peers_finish(tmp_path):
    scripts = {
        "A": [{"kind": "handoff", "to": "B", "text": "互评"}, {"kind": "ready"}],
        "B": [{"kind": "handoff", "to": "A", "text": "请确认"}, finish],
    }
    with team(tmp_path, scripts, max_agent_tokens=5) as (env, sandbox):
        original = sandbox.turn

        async def measured(member, prompt, turn):
            result = await original(member, prompt, turn)
            # A 每回合扣 3，B 每回合扣 1；两人的缓存命中均保留在原始用量中。
            return {
                **result,
                "tokens": 103 if member.id == "A" else 101,
                "cached_tokens": 100,
            }

        sandbox.turn = measured
        result = await drive(env)
        assert result["status"] == "budget_exhausted"
        assert result["submissions"]["idea"]["actor"] == "B"
        assert result["members"]["A"]["budget_exhausted"]
        assert not result["members"]["B"]["budget_exhausted"]
        assert [actor for actor, *_ in sandbox.seen] == ["A", "B", "A", "B"]
        assert sandbox.seen[-1][1]["unavailable_members"] == ["A"]
        assert sandbox.seen[-1][1]["wake_reason"] == "budget_return"
        from service import budget

        with env.store.transaction() as db:
            assert budget.agent_used(db, "A") == 6
            assert budget.agent_used(db, "B") == 2
            assert budget.total(db) == 408


async def test_exact_worker_limit_honors_last_delivery(tmp_path):
    with team(
        tmp_path, {"A": [{"kind": "ready"}], "B": [finish]}, max_agent_tokens=3
    ) as (env, _):
        result = await drive(env)
        assert result["status"] == "completed"
        assert result["submissions"]["idea"]["actor"] == "B"
    with team(
        tmp_path / "missing",
        {"A": [{"kind": "ready"}], "B": [{"kind": "ready"}]},
        max_agent_tokens=2,
    ) as (env, _):
        result = await drive(env)
        assert result["status"] == "budget_exhausted"
        assert "idea" in result["error"]
        assert all(m["budget_exhausted"] for m in result["members"].values())


async def test_native_harness_budget_error_only_retires_its_worker(tmp_path):
    from runtime.envs.team import HarnessError
    from service import budget
    from service.events import emit

    with team(tmp_path, {"B": [finish]}, max_agent_tokens=5) as (env, sandbox):
        original = sandbox.turn

        async def native_turn(member, prompt, turn):
            if member.id == "A":
                with env.store.transaction() as db:
                    budget.record(db, "A", "worker", 106, cached_tokens=100)
                    emit(db, "budget_exhausted", actor="A", error="成员用量超限")
                raise HarnessError("网关拒绝请求")
            return await original(member, prompt, turn)

        sandbox.turn = native_turn
        result = await drive(env)
        assert result["turns"] == 2
        assert result["status"] == "budget_exhausted"
        assert result["submissions"]["idea"]["actor"] == "B"
        assert result["members"]["A"]["budget_exhausted"]
        assert not result["members"]["B"]["budget_exhausted"]
