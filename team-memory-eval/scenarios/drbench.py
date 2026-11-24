"""DRBench App 场景直接挂接官方 DRBA 的工具注册表。"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from uuid import uuid4

from scenarios import Task, official_package
from service.gateway import endpoint

APP_IMAGE = "ghcr.io/mmunozm/drbench-services@sha256:62355c66022a5208767f534152e27432722c9dd0a4154891355e6d2580b51bb5"
# 固定的 amd64 清单中声明的 image config，同一镜像从 OCI 归档导入时也可识别。
APP_IMAGE_ID = "sha256:cf47a9c2172c68a4ee1a6c2423ae779957af585b3ef9bdbaeb0e21fb2ec2121e"


def load(spec, fetch):
    prefix = f"drbench/data/tasks/{spec['upstream_id']}/"
    question = json.loads(fetch(prefix + "config/task.json").read_text())
    # 装载应用前逐项检查题目文件。原件和评测标注都不挂载给成员。
    for entry in json.loads(fetch(prefix + "config/env.json").read_text())["env_files"]:
        path = entry["source"]
        if not path.startswith(prefix + "files/"):
            raise ValueError("环境材料不属于此题")
        fetch(path)
    public = {k: question[k] for k in ("company_info", "persona", "date", "citation_format")}
    if isinstance(public["persona"], dict):
        public["persona"] = {k: v for k, v in public["persona"].items() if k != "password"}
    return Task(
        "drbench", question["task_id"], spec["revision"], question["dr_question"],
        "Return report_text and report_insights with claim and citations.\n"
        + json.dumps(public, ensure_ascii=False),
        [], {"drbench_app": "Official DRBench enterprise application and research tools."},
        spec["outputs"], reference={
            "task_config": question,
            "eval_config": json.loads(fetch(prefix + "config/eval.json").read_text()),
        }, role_source="planner",
    )


@contextmanager
def app_tools(task, config, directory):
    official_package("drbench")
    from drbench import config as upstream, task_loader
    from drbench.agents.drbench_agent.action_planning_system import Action, ActionType
    from drbench.agents.drbench_agent.agent_tools.base import ResearchContext
    from drbench.agents.drbench_agent.drbench_agent import DrBenchAgent
    from drbench.drbench_enterprise_space import DrBenchEnterpriseSearchSpace
    from docker.errors import ImageNotFound

    base, headers = endpoint(config.provider, config.base_url, config.api_key_env,
                             api_key=config.api_key, chat=True)
    key = headers.get("authorization", "").removeprefix("Bearer ") or "unused"
    env = DrBenchEnterpriseSearchSpace(
        task=task_loader.get_task_from_id(task.id).get_path(), auto_ports=True,
        config={"container_name": "tme-drbench", "image": APP_IMAGE},
    )
    # 只向本机发布随机应用端口；不要触发上游可选的端口清理。
    ports = env.container_manager.docker_params["ports"]
    env.container_manager.docker_params["ports"] = {p: ("127.0.0.1", None) for p in ports}
    old_url, old_key = upstream.OPENROUTER_API_URL, upstream.OPENROUTER_API_KEY
    old_tmp = tempfile.tempdir
    scratch = tempfile.TemporaryDirectory(prefix="tme-drbench-app-", dir="/tmp")
    previous = {k: os.environ.get(k) for k in ("OPENROUTER_API_URL", "OPENROUTER_API_KEY", "SERPER_API_KEY")}
    sessions = {}
    lock = RLock()

    def session():
        root = Path(directory).resolve() / "drbench-tools" / uuid4().hex
        agent = DrBenchAgent(
            model="openrouter/" + config.model, workspace_dir=str(root / "work"),
            vector_store_base_dir=str(root / "index"),
            embedding_model="openrouter/text-embedding-ada-002", verbose=False,
        )
        agent._register_enterprise_tools(env)
        context = ResearchContext(original_question=task.goal, vector_store=agent.vector_store)
        return agent, context, RLock()

    def operation(name):
        def call(actor, args):
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("DRBench 工具需要非空 query")
            with lock:
                if actor not in sessions:
                    sessions[actor] = sessions.pop(None) if None in sessions else session()
                agent, context, actor_lock = sessions[actor]
            # 同一成员的向量库和上下文需要串行写，不同成员可同时检索。
            with actor_lock:
                action = Action(id=uuid4().hex, type=action_types[name], description=query,
                                parameters={"query": query}, preferred_tool=name)
                result = agent._execute_action(action, context)
                context.add_finding("action_" + action.id, result, category=action.type.value)
                return result
        return call

    try:
        tempfile.tempdir = scratch.name
        for digest in (APP_IMAGE.split("@")[1], APP_IMAGE_ID):
            try:
                image = env.container_manager.client.images.get(digest)
            except ImageNotFound:
                continue
            env.container_manager.docker_params["image"] = image.id
            break
        env.start()
        upstream.OPENROUTER_API_URL, upstream.OPENROUTER_API_KEY = base, key
        os.environ.update(OPENROUTER_API_URL=base, OPENROUTER_API_KEY=key)
        if config.serper_api_key is not None:
            os.environ["SERPER_API_KEY"] = config.serper_api_key.get_secret_value()
        sessions[None] = session()
        agent, _, _ = sessions[None]
        registry = {type(t).__name__: t for t in agent.tool_registry.select_tools()}
        action_types = {
            "EnterpriseAPITool": ActionType.ENTERPRISE_API, "InternetSearchTool": ActionType.WEB_SEARCH,
            "EnhancedURLFetchTool": ActionType.URL_FETCH, "SmartAnalysisTool": ActionType.DATA_ANALYSIS,
        }
        if registry.keys() != action_types.keys():
            raise ValueError(f"官方 App 工具注册表发生变化：{list(registry)}")
        task.operations = {name: ("drbench_app", operation(name)) for name in registry}
        task.operation_descriptions = {name: tool.purpose for name, tool in registry.items()}
        task.resources["drbench_app"] = 'Each tool takes {"query": "your request"}.\n' + "\n".join(
            f"{name}: {description}" for name, description in task.operation_descriptions.items()
        )
        yield {
            "mode": "App", "image": APP_IMAGE, "tools": list(registry),
            "serper_configured": bool(os.environ.get("SERPER_API_KEY")),
            "tool_model": config.model, "embedding_model": "text-embedding-ada-002",
        }
    finally:
        tempfile.tempdir = old_tmp
        scratch.cleanup()
        upstream.OPENROUTER_API_URL, upstream.OPENROUTER_API_KEY = old_url, old_key
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if env.container_manager.container is not None:
            env.delete()
        env.container_manager.client.close()
