import fcntl
import json
import os
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from functools import lru_cache, partial
from threading import Lock

from scenarios import RUNTIME_CACHE, Task, official_package
from service.store import write_json

ACTIONS = (
    "get_related_papers",
    "get_recent_papers",
    "collect_publications_and_coauthors",
    "get_paper_by_keyword",
    "get_paper_by_arxiv_id",
    "get_paper_by_title",
    "fetch_webpage",
)
ARXIV_RATE_STATE = RUNTIME_CACHE / "arxiv-rate.json"
ARXIV_INTERVAL = 3
ARXIV_SEARCH_TIMEOUT = 150


def arxiv_retry_delay(value, now, streak):
    if value:
        value = value.strip()
        try:
            if value.isascii() and value.isdecimal():
                return int(value)
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0, date.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            pass
    return min(60, ARXIV_INTERVAL * 2 ** min(streak, 5))


def rate_limited_arxiv_get(state_path, get, deadline, url, **kwargs):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {**kwargs.pop("headers", {}), "Connection": "close"}
    while True:
        with state_path.with_suffix(".lock").open("a") as lock:
            # 整个响应读取都在锁内，等待冷却时释放锁。
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                state = {"next_at": 0, "streak": 0}
            now = time.time()
            wait = max(0, state["next_at"] - now)
            remaining = deadline - time.monotonic()
            if wait >= remaining:
                reason = "HTTP 429 冷却期" if wait and state["streak"] else "共享请求队列"
                raise TimeoutError(f"arXiv 请求在{reason}内超时")
            if not wait:
                state["next_at"] = now + ARXIV_INTERVAL
                write_json(state_path, state)
                result = None
                try:
                    result = get(
                        url, headers=headers, timeout=min(25, remaining),
                        allow_redirects=False, **kwargs,
                    )
                finally:
                    now = time.time()
                    delay = ARXIV_INTERVAL
                    if result is not None and result.status_code == 429:
                        state["streak"] += 1
                        retry_after = arxiv_retry_delay(
                            result.headers.get("Retry-After"), now, state["streak"]
                        )
                        delay = max(delay, retry_after)
                    elif result is not None:
                        state["streak"] = 0
                    state["next_at"] = now + delay
                    write_json(state_path, state)
                return result
        time.sleep(wait)


def search_arxiv(search):
    import arxiv

    client = arxiv.Client(delay_seconds=0)
    deadline = time.monotonic() + ARXIV_SEARCH_TIMEOUT
    # 固定版本的客户端通过此 session 获取每页，也用它发送重试。
    with client._session as session:
        session.get = partial(rate_limited_arxiv_get, ARXIV_RATE_STATE, session.get, deadline)
        yield from client.results(search)


class KeywordExtractor:
    def __init__(self):
        self.lock = Lock()
        self.model = None

    def extract_keywords(self, *args, **kwargs):
        with self.lock:
            if self.model is None:
                from keybert import KeyBERT

                self.model = KeyBERT()
            return self.model.extract_keywords(*args, **kwargs)


keyword_extractor = KeywordExtractor()


def research_environment(config):
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    for key, directory in (
        ("HF_HOME", "hf"),
        ("TORCH_HOME", "torch"),
        ("MPLCONFIGDIR", "mpl"),
        ("XDG_CACHE_HOME", ""),
    ):
        os.environ[key] = str(RUNTIME_CACHE / directory)
    official_package("marble")
    from marble.environments import research_env
    from marble.environments.research_utils import paper_collector
    from scenarios import marble_web

    # 关键词模型的锁只覆盖加载和提取，检索使用独立的宿主限速。
    paper_collector.KeyBERT = lambda: keyword_extractor
    paper_collector.perform_arxiv_search = search_arxiv
    # 保留上游网页处理器，只在其 HTTP 连接处执行宿主的公网边界。
    research_env.requests = marble_web
    environment = research_env.ResearchEnvironment(config)

    # 固定版本的三种搜索返回 arxiv.Result，原处理器直接调用 model_dump 会失败。
    def serialize(result):
        return {
            "title": result.title,
            "abstract": result.summary.replace("\n", " "),
            "authors": [author.name for author in result.authors],
            "arxiv_id": result.get_short_id().split("v", 1)[0],
            "url": result.entry_id,
            "domain": result.primary_category,
            "timestamp": int(result.published.timestamp()),
        }

    def by_keyword(keyword, max_papers):
        papers = paper_collector.get_paper_by_keyword(keyword, set(), max_papers)
        return {"success": True, "papers": [serialize(paper) for paper in papers]}

    def single_paper(paper):
        if paper is None:
            return {"success": False, "error-msg": "Paper not found."}
        return {"success": True, "paper": serialize(paper)}

    def by_arxiv_id(arxiv_id):
        return single_paper(paper_collector.get_paper_by_arxiv_id(arxiv_id))

    def by_title(title):
        return single_paper(paper_collector.get_paper_by_title(title))

    # 工具定义中的 domain 是可选项，原版处理器需要显式传入 None。
    def recent(max_results, domain=None):
        papers = paper_collector.get_recent_papers(domain=domain, max_results=max_results)
        return {"success": True, "papers": [paper.model_dump(exclude_none=True) for paper in papers]}

    for name, handler in (
        ("get_paper_by_keyword", by_keyword),
        ("get_paper_by_arxiv_id", by_arxiv_id),
        ("get_paper_by_title", by_title),
        ("get_recent_papers", recent),
    ):
        environment.register_action(name, handler, environment.action_handler_descriptions[name])
    return environment


def load(spec, fetch):
    with fetch("multiagentbench/research/research_main.jsonl").open() as stream:
        for row in map(json.loads, stream):
            if row["task_id"] == spec["upstream_id"]:
                break
        else:
            raise ValueError(
                f"当前 MARBLE 数据版本中找不到 research/{spec['upstream_id']}"
            )
    roles = [
        {
            "id": a["agent_id"],
            "role": a["agent_id"],
            "description": a["profile"],
            "resource_ids": ["research"],
        }
        for a in row["agents"]
    ]

    @lru_cache(maxsize=1)
    def environment():
        return research_environment(row["environment"])

    def action(name):
        return lambda actor, args: environment().apply_action(actor, name, args)

    operations = {name: ("research", action(name)) for name in ACTIONS}
    return Task(
        "marble",
        f"research/{row['task_id']}",
        spec["revision"],
        row["task"]["content"],
        row["task"]["output_format"],
        roles,
        {
            "research": 'Use python -m tools.client call get_paper_by_keyword \'{"keyword":"research term","max_papers":3}\' for paper search. '
            "Other operations include get_recent_papers(domain,max_results), get_related_papers(query,num_results), "
            "get_paper_by_title(title), get_paper_by_arxiv_id(arxiv_id), collect_publications_and_coauthors(author). "
            "Use fetch_webpage(url) to read a public web page. "
            "The task introduction and output format are in the public goal. No enterprise files are mounted."
        },
        spec["outputs"],
        operations=operations,
        reference={"metrics": row["metrics"]},
        role_source=spec.get("role_source", "upstream"),
    )
