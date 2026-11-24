"""在历史兼容源码上回放已经接受的动作，用输入摘要和最终文件验证每道题。"""
import argparse
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from runtime.envs.point_to_point import PointToPointEnv
from scenarios import load
from service import archive
from service.store import Store


def digest(value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


async def replay(case, directory):
    trace = json.loads((case / "trajectory.json").read_text())
    plan = json.loads((case / "plan.json").read_text())
    settings = json.loads((case / "settings.json").read_text())
    task = load(trace["benchmark"], trace["task"])
    task.resources = trace["resources"]
    store = Store(directory)
    remaining = iter(trace["turns"])

    def planner(prompt):
        if digest(prompt) != trace["planner_input_sha256"]:
            raise ValueError("Planner 输入与记录不同")
        return json.dumps(plan)

    class RecordedMember:
        def prepare(self, member):
            pass

        async def turn(self, member, prompt, turn_id):
            turn = next(remaining, None)
            if not turn or member.id != turn["actor"] or digest(prompt) != turn["input_sha256"]:
                raise ValueError(f"{task.id} 的回合输入与记录不同")
            with store.transaction() as db:
                for file in turn["uploads"]:
                    db.execute("INSERT INTO blobs VALUES(?,?,?)",
                               (file["id"], file["name"], (case / file["path"]).read_bytes()))
                    db.execute("INSERT INTO grants VALUES(?,?)", (file["id"], file["actor"]))
            return {"session_id": turn["session_id"], "action": turn["action"], "metered": True}

    try:
        env = PointToPointEnv(store, RecordedMember(), planner, max_turns=settings["max_turns"],
                              max_agent_tokens=settings["max_agent_tokens"])
        env.start(task, settings["seed"])
        while env.running:
            await env.step()
        result = env.result()
        if result["status"] != trace["execution"] or next(remaining, None) is not None:
            raise ValueError(f"{task.id} 终态或回合数不符")
        slot = "report.json" if task.benchmark == "drbench" else "idea"
        submission = result["submissions"][slot]
        with store.transaction() as db:
            data = archive.read(db, submission["actor"], submission["blob"])["data"]
        if data != (case / slot).read_bytes():
            raise ValueError(f"{task.id} 交付文件与原件不同")
        print(case.parent.name, task.id, result["status"], result["turns"], hashlib.sha256(data).hexdigest())
    finally:
        store.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("evidence", type=Path)
    base = p.parse_args().evidence
    records = json.loads((base / "index.json").read_text())
    for row in records:
        if row["method"] != "p2p" or row["scoring"] != "scored":
            continue
        case = (base / row["record"]).parent
        with tempfile.TemporaryDirectory(prefix="tme-work-replay-") as tmp:
            asyncio.run(replay(case, tmp))
