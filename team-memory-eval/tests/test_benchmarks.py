import json

import pytest

from run import Config, execute
from scenarios import SOURCES, drbench, load
from tests.fakes import ScriptedSandbox, fixed, model_server, sample_task
from tests.test_team import finish


def _batch_worker(barrier, config, out):
    out.mkdir(parents=True)
    barrier.wait(timeout=20)
    if config.task_id == "research/2":
        raise SystemExit(1)
    (out / "finished").write_text(config.task_id)


def test_batch_starts_all_tasks_and_keeps_siblings_after_failure(tmp_path, monkeypatch):
    import multiprocessing
    from functools import partial
    from types import SimpleNamespace

    import run

    barrier = multiprocessing.get_context("spawn").Barrier(3)
    monkeypatch.setattr(run, "run_child", partial(_batch_worker, barrier))
    configs = [SimpleNamespace(task_id=f"research/{n}") for n in (1, 2, 3)]
    assert run.run_batch(configs, tmp_path) == 1
    directories = sorted(tmp_path.iterdir())
    assert len(directories) == 3
    assert [p.name.split("-", 1)[1] for p in directories] == [
        "research-1", "research-2", "research-3"
    ]
    assert {marker.read_text() for marker in tmp_path.glob("*/finished")} == {
        "research/1", "research/3"
    }


def test_marble_batch_expands_only_task_ids():
    import yaml

    from scenarios import ROOT

    data = yaml.safe_load((ROOT / "experiments/marble-api.example.yaml").read_text())
    data["harness"] = "codex"
    data["member_reasoning_effort"] = "high"
    single = Config.model_validate(data)
    assert Config.for_tasks(data) == [single]
    del data["task_id"]
    configs = Config.for_tasks({**data, "task_ids": [3, 1, 2]})
    assert configs == [
        single.model_copy(update={"task_id": f"research/{number}"}) for number in (3, 1, 2)
    ]
    for fields in (
        {"task_ids": []}, {"task_ids": [1, 1]}, {"task_ids": [True]},
        {"task_ids": [0]}, {"task_ids": "1, 2, 3"}, {"task_ids": ["2"]},
        {"task_ids": [1], "task_id": "research/2"},
        {"task_ids": [1], "benchmark": "drbench"},
    ):
        with pytest.raises(ValueError, match="task_ids"):
            Config.for_tasks({**data, **fields})


def test_member_reasoning_effort_validation():
    import yaml

    from scenarios import ROOT

    data = yaml.safe_load((ROOT / "experiments/marble-api.example.yaml").read_text())
    assert Config.model_validate(data).member_reasoning_effort is None
    for harness, effort in (("codex", "xhigh"), ("claude-code", "medium"), ("deepseek", "off")):
        config = Config.model_validate({**data, "harness": harness, "member_reasoning_effort": effort})
        assert config.model_dump()["member_reasoning_effort"] == effort
    for harness, effort in (("deepseek", "medium"), ("claude-code", "off"), ("codex", "")):
        with pytest.raises(ValueError, match="member_reasoning_effort"):
            Config.model_validate({**data, "harness": harness, "member_reasoning_effort": effort})


async def test_user_stop_persists_cancelled_run_without_grading(tmp_path):
    import asyncio

    import yaml

    from scenarios import ROOT

    task = sample_task()
    config = Config.model_validate(
        yaml.safe_load((ROOT / "experiments/marble.yaml").read_text())
    )
    entered = asyncio.Event()

    class Planner:
        def __init__(self, *_):
            pass

        def complete(self, _):
            return json.dumps(fixed(task))

    class Waiting(ScriptedSandbox):
        async def turn(self, member, prompt, turn_id):
            with self.service.lease(member):
                entered.set()
                await asyncio.Future()

    directory = tmp_path / "stopped"
    run = asyncio.create_task(
        execute(
            config,
            directory,
            loader=lambda *_: task,
            gateway_class=Planner,
            sandbox_class=lambda d, c, s: Waiting(d, c, s, {}),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    run.cancel()
    result, scoring = await run
    assert result["status"] == "cancelled" and scoring["status"] == "not_run"
    assert json.loads((directory / "result.json").read_text())["status"] == "cancelled"


def test_marble_research_task_selection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from experiments import run_codex_research
    from scenarios import marble

    repo = tmp_path / "MARBLE"
    monkeypatch.setenv("TME_REFERENCE_REPOS", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="缺少参考仓库"):
        load("marble", "research/2")
    (repo / ".git").mkdir(parents=True)
    revision = "wrong"

    def checkout_head(args, *, text):
        assert args == ["git", "-C", str(repo), "rev-parse", "HEAD"]
        return revision

    monkeypatch.setattr("scenarios.subprocess.check_output", checkout_head)
    with pytest.raises(ValueError, match="版本不匹配"):
        load("marble", "research/2")
    revision = SOURCES["marble"][1]
    dataset = repo / "multiagentbench/research/research_main.jsonl"
    dataset.parent.mkdir(parents=True)
    with dataset.open("w") as stream:
        for number in (1, 2):
            row = {
                "task_id": number,
                "task": {"content": f"idea {number}?", "output_format": "5q"},
                "agents": [{"agent_id": "A", "profile": "writer"}],
                "metrics": {},
                "environment": {},
            }
            stream.write(json.dumps(row) + "\n")
    m = load("marble", "research/2")
    assert (m.id, m.goal, m.format) == ("research/2", "idea 2?", "5q")
    assert m.outputs == {"idea": "text"}
    assert m.roles == [
        {"id": "A", "role": "A", "description": "writer", "resource_ids": ["research"]}
    ]
    assert tuple(m.operations) == marble.ACTIONS and len(m.operations) == 7
    expected = ("A", "fetch_webpage", {"url": "https://example.org"})
    descriptions = {
        k: {"function": {"description": k, "parameters": {"type": "object"}}} for k in marble.ACTIONS
    }
    official = SimpleNamespace(apply_action=lambda *args: args, action_handler_descriptions=descriptions)
    monkeypatch.setattr(marble, "research_environment", lambda config: official)
    monkeypatch.setattr(run_codex_research, "research_environment", lambda config: official)
    assert m.operations["fetch_webpage"][1]("A", expected[2]) == expected
    assert [tool["name"] for tool in run_codex_research.research_catalog(m)] == list(m.operations)
    with pytest.raises(ValueError, match="research/101"):
        load("marble", "research/101")
    with pytest.raises(ValueError, match="research/N"):
        load("marble", "research/two")


def test_marble_keyword_model_is_shared_across_threads(monkeypatch):
    import sys
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from types import SimpleNamespace

    from scenarios.marble import KeywordExtractor

    starts = []
    ready = Barrier(5)
    active = Lock()

    class Model:
        def __init__(self):
            starts.append(1)

        def extract_keywords(self, query):
            assert active.acquire(blocking=False), "关键词模型收到并发调用"
            try:
                time.sleep(0.02)
                return [query]
            finally:
                active.release()

    monkeypatch.setitem(sys.modules, "keybert", SimpleNamespace(KeyBERT=Model))
    extractor = KeywordExtractor()

    def search(query):
        ready.wait(timeout=5)
        return extractor.extract_keywords(query)

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert list(pool.map(search, range(5))) == [[i] for i in range(5)]
    assert len(starts) == 1


def test_marble_arxiv_respects_retry_after_and_shared_backoff(tmp_path, monkeypatch):
    from email.utils import formatdate
    from types import SimpleNamespace

    from scenarios import marble

    now = 1000

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(marble, "time", SimpleNamespace(time=clock, monotonic=clock, sleep=sleep))
    responses = iter((
        (429, "7"), (429, formatdate(1020, usegmt=True)), (429, "invalid"),
        (200, None), (429, None),
    ))
    started = []

    def get(_url, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["headers"]["Connection"] == "close"
        started.append(now)
        status, retry_after = next(responses)
        return SimpleNamespace(status_code=status, headers={"Retry-After": retry_after})

    args = (tmp_path / "rate.json", get)
    for status in (429, 429, 429, 200, 429):
        result = marble.rate_limited_arxiv_get(*args, 1100, "https://example.invalid/")
        assert result.status_code == status
    assert started == [1000, 1007, 1020, 1044, 1047]
    assert json.loads(args[0].read_text()) == {"next_at": 1053, "streak": 1}
    with pytest.raises(TimeoutError, match="HTTP 429"):
        marble.rate_limited_arxiv_get(*args, 1050, "https://example.invalid/")
    assert now == 1047


def test_marble_arxiv_queue_serializes_separate_processes(tmp_path, monkeypatch):
    import time
    from multiprocessing import get_context
    from types import SimpleNamespace

    from scenarios import marble

    monkeypatch.setattr(marble, "ARXIV_INTERVAL", 0.05)
    context = get_context("fork")
    ready = context.Barrier(2)
    requests = context.Queue()

    def request_page():
        def get(_url, **_kwargs):
            start = time.monotonic()
            time.sleep(0.03)
            requests.put((start, time.monotonic()))
            return SimpleNamespace(status_code=200)

        ready.wait(timeout=5)
        marble.rate_limited_arxiv_get(
            tmp_path / "rate.json", get, time.monotonic() + 5, "https://example.invalid/"
        )

    children = [context.Process(target=request_page) for _ in range(2)]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(timeout=6)
            assert child.exitcode == 0
        first, second = sorted(requests.get(timeout=1) for _ in children)
        assert second[0] - first[1] >= 0.05
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join()
        requests.close()


def test_drbench_task_shape(tmp_path):
    def write(name, value):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))

    pre = "drbench/data/tasks/DR0002/"
    write(
        pre + "config/task.json",
        {
            "task_id": "DR0002",
            "dr_question": "why?",
            "company_info": "AC",
            "persona": "lead",
            "date": "today",
            "citation_format": "urls",
        },
    )
    write(
        pre + "config/env.json",
        {
            "env_files": [
                {
                    "source": pre + "files/one.txt",
                    "destination": "shared/x.txt",
                    "qa_type": "insight",
                }
            ]
        },
    )
    write(pre + "files/one.txt", "visible")
    write(pre + "config/eval.json", {"secret": "hidden"})
    import yaml
    from scenarios import ROOT

    spec = yaml.safe_load((ROOT / "tasks/drbench/research.yaml").read_text())
    d = drbench.load(
        {
            **spec,
            "upstream_id": "DR0002",
            "revision": "pin",
            "outputs": {"report": "report_json"},
        },
        lambda n: tmp_path / n,
    )
    assert d.roles == []
    assert d.role_source == "planner"
    assert d.materials == {} and set(d.resources) == {"drbench_app"}
    assert "hidden" not in json.dumps(d.public()) and "x.txt" not in json.dumps(d.public())
    assert d.reference["eval_config"] == {"secret": "hidden"}

    from experiments.run_codex_research import prompt_for
    from runtime.planner import plan

    members = [
        {"id": role, "role": role, "assignment": role, "resource_ids": ["drbench_app"]}
        for role in ("sales", "operations", "writer")
    ]
    planned = {"members": members, "first_member": "operations"}

    def complete(prompt):
        assert "fixed_roles" not in prompt
        assert "Choose at least two complementary roles" in prompt
        return json.dumps(planned)

    team = plan(d, complete)
    assert [m.id for m in team.members] == ["sales", "operations", "writer"]
    assert team.first_member == "operations"
    members[2]["role"] = "SALES"
    with pytest.raises(ValueError, match="岗位"):
        plan(d, complete)
    members[2]["role"] = "writer"
    members[2]["resource_ids"] = []
    with pytest.raises(ValueError, match="工具权限"):
        plan(d, complete)
    planned.update(members=members[:1], first_member="sales")
    with pytest.raises(ValueError, match="至少需要两名"):
        plan(d, lambda _: json.dumps(planned))
    assert "spawn native subagents" in prompt_for(d)


def test_native_roles_come_from_the_root_session_tree(tmp_path):
    from experiments.run_codex_research import native_team

    sessions = tmp_path / "native-sessions"
    sessions.mkdir()
    for child, parent in [("lead", "root"), ("analyst", "lead"), ("foreign", "other")]:
        spawn = {"parent_thread_id": parent, "agent_path": f"/{child}"}
        payload = {"id": child, "source": {"subagent": {"thread_spawn": spawn}}}
        (sessions / f"rollout-{child}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n"
        )
    assert [m["thread_id"] for m in native_team(tmp_path, "root")] == ["lead", "analyst"]


def test_drbench_batch_selects_same_questions_for_both_runners():
    import yaml
    from scenarios import ROOT

    data = yaml.safe_load((ROOT / "experiments/drbench-api.example.yaml").read_text())
    for runner in ("tme", "codex-native"):
        configs = Config.for_tasks({**data, "runner": runner, "serper_api_key": "private-test-key"})
        assert [c.task_id for c in configs] == ["DR0001", "DR0002", "DR0003"]
        assert all(c.planner_mode == "generated" and c.runner == runner for c in configs)
        assert all("serper_api_key" not in c.model_dump() and "private-test-key" not in repr(c) for c in configs)
    assert Config.for_tasks({k: v for k, v in data.items() if k != "planner_mode"})[0].planner_mode == "generated"
    with pytest.raises(ValueError, match="DRBench 使用"):
        Config.for_tasks({**data, "planner_mode": "fixed"})
    for invalid in (["DR0001", "DR0001"], ["DR1"], [1], ["../DR0001"]):
        with pytest.raises(ValueError, match="task_ids"):
            Config.for_tasks({**data, "task_ids": invalid})


@pytest.mark.parametrize("harness", ["deepseek", "claude-code"])
async def test_full_entry_and_score_error(tmp_path, harness):
    t = sample_task()
    config = Config(
        benchmark="marble",
        task_id="research/1",
        harness=harness,
        model="scripted",
        planner_mode="fixed",
        protocol="point_to_point",
        seed=7,
        max_turns=5,
        max_team_tokens=1000,
        judge_model="fixture",
        provider="deepseek" if harness == "deepseek" else "anthropic",
        judge_provider="openai",
        api_key="worker-only-test-secret",
        judge_api_key="judge-only-test-secret",
    )
    with model_server(lambda _: json.dumps(fixed(t))) as (url, calls):
        config = config.model_copy(update={"base_url": url})

        def sandbox(d, c, s):
            return ScriptedSandbox(
                d,
                c,
                s,
                {
                    "A": [
                        {"kind": "handoff", "to": "B", "text": "please", "ready": True}
                    ],
                    "B": [finish],
                },
            )

        def grade(task, files, model):
            assert files == {"idea": b"result"}
            raise RuntimeError("judge offline")

        out = tmp_path / "run"
        result, scores = await execute(
            config,
            out,
            loader=lambda *_: t,
            sandbox_class=sandbox,
            grader=grade,
        )
    assert result["status"] == "completed" and scores["status"] == "evaluation_error"
    assert len(calls) == 1 and "judge offline" in scores["error"]
    for artifact in out.rglob("*"):
        if artifact.is_file():
            data = artifact.read_bytes()
            assert b"worker-only-test-secret" not in data
            assert b"judge-only-test-secret" not in data
    assert all(
        (out / f).exists()
        for f in (
            "run.json",
            "plan.json",
            "result.json",
            "scores.json",
            "state.sqlite",
            "events.jsonl",
            "submissions/idea",
        )
    )
