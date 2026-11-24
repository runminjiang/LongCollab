import json

import pytest

from service import archive, messages
from service.store import Store
from tests.fakes import sample_task


def test_inbox_reads_legacy_text_and_new_messages(tmp_path):
    store = Store(tmp_path)
    try:
        with store.transaction() as db:
            legacy = {"from": "A", "text": "旧正文", "files": []}
            db.execute(
                "INSERT INTO mail(sender,target,body) VALUES(?,?,?)",
                ("A", "B", json.dumps(legacy)),
            )
            messages.send(db, "A", "B", "新正文")
            inbox = messages.inbox(db, "B")
            assert [m["message"] for m in inbox] == ["旧正文", "新正文"]
            assert all("text" not in m for m in inbox)
            assert (
                json.loads(db.execute("SELECT body FROM mail WHERE id=1").fetchone()[0])
                == legacy
            )
    finally:
        store.close()


def test_blob_grants_atomic_with_version(tmp_path):
    s = Store(tmp_path)
    t = sample_task()
    try:
        with s.transaction() as db:
            key = archive.upload(db, "A", "draft.txt", b"one")
            archive.submit(db, t, "A", "idea", key, "1")
        with pytest.raises(ValueError), s.transaction() as db:
            messages.send(db, "A", "B", "check", [key, "bogus"])
        with s.transaction() as db:
            assert not messages.inbox(db, "B")
            with pytest.raises(ValueError):
                archive.read(db, "B", key)
            messages.send(db, "A", "B", "ok", [key])
            assert archive.read(db, "B", key)["data"] == b"one"
            other = archive.upload(db, "B", "new.txt", b"two")
            archive.submit(db, t, "B", "idea", other, "2")
            assert archive.latest(db)["idea"]["actor"] == "B"
            assert db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 2
            t.owners["idea"] = "A"
            with pytest.raises(ValueError):
                archive.submit(db, t, "B", "idea", other, "3")
    finally:
        s.close()


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"report_text": "x", "report_insights": None},
        {"report_text": "x", "report_insights": [{"claim": "x", "citations": "oops"}]},
    ],
)
def test_invalid_report(value):
    with pytest.raises(ValueError):
        archive.validate(json.dumps(value).encode(), "report_json")


def test_vllm_preserves_native_tools_and_reserves_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import httpx

    from service import budget
    from service.gateway import Gateway, usage_tokens
    from tests.fakes import envelope

    monkeypatch.setenv("TEST_SERVE_KEY", "validation-key")
    cfg = SimpleNamespace(
        provider="vllm",
        model="other-model",
        base_url="http://server/custom/v1",
        api_key_env="TEST_SERVE_KEY",
        max_team_tokens=120,
        max_output_tokens=256,
    )
    tool = {"name": "Bash", "input_schema": {"type": "object", "required": ["command"]}}
    calls = []

    def handler(req):
        assert (
            req.url.host == "server"
            and req.headers["authorization"] == "Bearer validation-key"
        )
        if req.method == "GET":
            assert req.url.path == "/custom/v1/models"
            return httpx.Response(
                200, json={"data": [{"id": cfg.model, "max_model_len": 1000}]}
            )
        data = json.loads(req.content)
        assert data["model"] == cfg.model and data["tools"] == [tool]
        assert data["system"] == [
            {"type": "text", "text": "SDK instructions"},
            {"type": "text", "text": "reminder"},
        ]
        assert data["messages"] == [{"role": "user", "content": "work"}]
        if req.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 90})
        assert req.url.path == "/custom/v1/messages"
        calls.append(data)
        typ, raw = envelope("anthropic", ("Bash", {"command": "pwd"}), True, cfg.model)
        return httpx.Response(200, headers={"content-type": typ}, content=raw)

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **opts: client(transport=httpx.MockTransport(handler), **opts),
    )
    payload = {
        "model": "from-sdk",
        "system": "SDK instructions",
        "messages": [
            {"role": "user", "content": "work"},
            {"role": "system", "content": "reminder"},
        ],
        "max_tokens": 32000,
        "stream": True,
        "tools": [tool],
        "tool_choice": {"type": "auto"},
    }
    store = Store(tmp_path)
    try:
        gw = Gateway(cfg, store)
        status, typ, raw = gw.forward("A", "worker", "/v1/messages", payload)
        assert status == 200 and typ == "text/event-stream" and b"tool_use" in raw
        assert usage_tokens(raw) == 13
        assert calls[0]["max_tokens"] == 30 and calls[0]["tool_choice"] == {
            "type": "auto"
        }
        assert not {"response_format", "chat_template_kwargs"} & calls[0].keys()
        with store.transaction() as db:
            assert budget.total(db) == 13
            budget.record(db, "B", "worker", 17)
        with pytest.raises(budget.Exhausted):
            gw.forward("A", "worker", "/v1/messages", payload)
        assert len(calls) == 1
    finally:
        store.close()


def test_vllm_planner_uses_its_own_model_and_template(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import httpx

    from service.gateway import Gateway

    cfg = SimpleNamespace(
        provider="vllm",
        model="member",
        planner_model="planner",
        base_url="http://host/v1",
        api_key_env=None,
        max_team_tokens=100,
        max_output_tokens=None,
        planner_chat_template_kwargs={"enable_thinking": False},
    )

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "planner", "max_model_len": 80}]}
            )
        body = json.loads(request.content)
        assert (
            body["model"] == "planner"
            and body["chat_template_kwargs"] == cfg.planner_chat_template_kwargs
        )
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 30})
        assert request.url.path == "/v1/chat/completions" and body["max_tokens"] == 50
        assert (
            body["response_format"] == {"type": "json_object"} and "tools" not in body
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"total_tokens": 32},
            },
        )

    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda self, request: handler(request)
    )
    store = Store(tmp_path)
    try:
        assert Gateway(cfg, store).complete("plan") == "{}"
    finally:
        store.close()


def test_endpoint_keys_are_bound_to_explicit_service(monkeypatch):
    from service.gateway import endpoint

    monkeypatch.setenv("OPENAI_API_KEY", "ambient")
    monkeypatch.setenv("TEAM_KEY", "selected")
    for provider in ("openai", "anthropic", "vllm"):
        base, auth = endpoint(provider, "https://other.test/custom/v1", None)
        assert base == "https://other.test/custom/v1" and not auth
        _, auth = endpoint(provider, base, "TEAM_KEY")
        assert auth == (
            {"x-api-key": "selected"}
            if provider == "anthropic"
            else {"authorization": "Bearer selected"}
        )
        with pytest.raises(ValueError, match="MISSING_KEY"):
            endpoint(provider, base, "MISSING_KEY")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("inline_keys", [False, True])
def test_openai_endpoint_native_tools_and_credentials(
    tmp_path, monkeypatch, stream, inline_keys
):
    import httpx
    import yaml

    from run import Config
    from scenarios import ROOT
    from service.gateway import Gateway
    from tests.fakes import envelope

    monkeypatch.setenv("TEST_SERVE_KEY", "worker-test-key")
    monkeypatch.setenv("TEST_JUDGE_KEY", "judge-test-key")
    spec = yaml.safe_load((ROOT / "experiments/marble-api.example.yaml").read_text())
    spec.update(
        provider="openai",
        member_reasoning_effort="high",
        model="namespace/model",
        planner_model="planner-model",
        base_url="https://serve.test/compat/v2",
        api_key_env="TEST_SERVE_KEY",
        max_output_tokens=300,
        max_team_tokens=10000,
        judge_provider="openai",
        judge_base_url="https://judge.test/v1",
        judge_api_key_env="TEST_JUDGE_KEY",
    )
    if inline_keys:
        spec.update(
            api_key="worker-test-key",
            judge_api_key="judge-test-key",
            api_key_env=None,
            judge_api_key_env=None,
        )
    cfg = Config.model_validate(spec)
    for key in ("api_key", "judge_api_key"):
        assert key not in cfg.model_dump()
    assert "worker-test-key" not in repr(cfg) and "judge-test-key" not in repr(cfg)

    def handler(request):
        data = json.loads(request.content)
        if request.url.host == "judge.test":
            assert "reasoning_effort" not in data
            assert (
                request.url.path == "/v1/chat/completions"
                and data["model"] == "judge-model"
            )
            assert request.headers["authorization"] == "Bearer judge-test-key"
            answer = "judged"
        else:
            assert str(request.url) == "https://serve.test/compat/v2/chat/completions"
            assert request.headers["authorization"] == "Bearer worker-test-key"
            if data["model"] == "planner-model":
                assert "reasoning_effort" not in data
                answer = "plan"
            else:
                assert data["reasoning_effort"] == "high"
                assert data["model"] == "namespace/model"
                assert data["tools"][0]["function"]["name"] == "Bash"
                assert (
                    not {"context_management", "dsh_session_log", "metadata", "user"}
                    & data.keys()
                )
                assert (
                    data["messages"][-1]["role"] == "tool"
                    and data["messages"][-1]["tool_call_id"] == "tool-1"
                )
                assert data["messages"][-2]["tool_calls"][0]["id"] == "tool-1"
                assert data["tool_choice"] == "auto" and "response_format" not in data
                answer = ("Bash", {"command": "pwd"})
        typ, raw = envelope("openai", answer, data.get("stream", False), data["model"])
        return httpx.Response(200, headers={"content-type": typ}, content=raw)

    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda self, request: handler(request)
    )
    store = Store(tmp_path)
    try:
        gw = Gateway(cfg, store)
        assert gw.complete("make a plan") == "plan"
        history = [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "pwd"},
                        "id": "tool-1",
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "output",
                    }
                ],
            },
        ]
        status, typ, raw = gw.forward(
            "A",
            "worker",
            "/v1/messages",
            {
                "messages": history,
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
                "dsh_session_log": "sdk-private-log",
                "context_management": {"edits": []},
                "metadata": {"user_id": "sdk-private-user"},
                "tool_choice": {"type": "auto"},
                "stream": stream,
                "max_tokens": 512,
            },
        )
        assert status == 200 and b"tool_use" in raw and b"pwd" in raw
        assert typ == ("text/event-stream" if stream else "application/json")
        if not stream:
            msg = json.loads(raw)
            assert (
                msg["content"][0]["input"] == {"command": "pwd"}
                and msg["stop_reason"] == "tool_use"
            )
        assert (
            gw.judge_chat(
                {"messages": [{"role": "user", "content": "score"}]}, "judge-model"
            )["choices"][0]["message"]["content"]
            == "judged"
        )
        with store.transaction() as db:
            assert (
                db.execute("SELECT tokens FROM usage WHERE actor='A'").fetchone()[0]
                == 13
            )
    finally:
        store.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_openai_planner_and_marble_judge_token_parameters(tmp_path, monkeypatch, legacy):
    from contextlib import closing

    import httpx
    import yaml

    from run import Config
    from scenarios import ROOT
    from service.gateway import Gateway
    from tests.fakes import envelope

    spec = yaml.safe_load((ROOT / "experiments/marble-api.example.yaml").read_text())
    cfg = Config.model_validate({**spec, "harness": "codex", "max_team_tokens": 2000})
    requests = []

    def serve(self, request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        unsupported = "max_completion_tokens" if legacy else "max_tokens"
        if unsupported in body:
            return httpx.Response(400, json={"error": {
                "code": "unsupported_parameter", "param": unsupported,
                "message": f"Unsupported parameter: '{unsupported}' is not supported with this model.",
            }})
        if not legacy and "temperature" in body:
            return httpx.Response(400, json={"error": {
                "code": "unsupported_value", "param": "temperature",
                "message": "Only the default temperature is supported.",
            }})
        typ, raw = envelope("openai", "ok", False, body["model"])
        return httpx.Response(200, headers={"content-type": typ}, content=raw)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve)
    field = "max_tokens" if legacy else "max_completion_tokens"
    with closing(Store(tmp_path)) as store:
        gw = Gateway(cfg, store)
        assert gw.complete("plan") == "ok"
        assert requests[0].get("max_completion_tokens") == 2000
        assert requests[-1][field] == 2000
        judge = {
            "messages": [{"role": "user", "content": "score"}],
            "max_tokens": 512, "temperature": 0, "n": 1,
        }
        assert gw.judge_chat(judge, "judge")["choices"][0]["message"]["content"] == "ok"
        assert requests[-1][field] == 512
        assert requests[-1].get("temperature") == (0 if legacy else None)
        assert "n" not in requests[-1] and len(requests) == 4
        with store.transaction() as db:
            usage = db.execute("SELECT category, tokens FROM usage ORDER BY id")
            assert [tuple(row) for row in usage] == [("planner", 13), ("judge", 13)]
            retries = db.execute("SELECT data FROM events WHERE kind='model_parameter_retry' ORDER BY seq")
            assert [json.loads(row[0])["param"] for row in retries] == [
                "max_completion_tokens" if legacy else "temperature"
            ] * 2


def test_yaml_auth_error_keeps_key_out_of_trace(tmp_path, monkeypatch):
    import httpx
    import yaml

    from run import Config
    from scenarios import ROOT
    from service.gateway import Gateway

    template = (ROOT / "experiments/marble-api.example.yaml").read_text()
    cfg = Config.model_validate(
        yaml.safe_load(template.replace("your-api-key", "test-key-401"))
    )
    assert cfg.api_key == cfg.judge_api_key

    def reject(self, request):
        assert request.headers["authorization"] == "Bearer test-key-401"
        return httpx.Response(401, json={"error": "invalid key test-key-401"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject)
    store = Store(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="planner HTTP 401") as error:
            Gateway(cfg, store).complete("test")
        with store.transaction() as db:
            logged = db.execute(
                "SELECT data FROM events WHERE kind='model_error'"
            ).fetchone()[0]
        for message in (str(error.value), logged):
            assert "test-key-401" not in message and "[REDACTED]" in message
    finally:
        store.close()


def test_default_configs_allow_independent_harness_and_model():
    import yaml
    from pydantic import ValidationError

    from run import Config
    from scenarios import ROOT

    for filename in ("marble.yaml", "drbench.yaml"):
        spec = yaml.safe_load((ROOT / "experiments" / filename).read_text())
        default = Config.model_validate(spec)
        assert default.provider == default.judge_provider == "vllm"
        assert default.model == default.judge_model == "qwen3.5-9b"
        assert default.base_url == default.judge_base_url == "http://127.0.0.1:8505/v1"
        for harness in ("deepseek", "claude-code"):
            for provider in ("vllm", "openai", "anthropic", "local"):
                cfg = Config.model_validate(
                    {
                        **spec,
                        "harness": harness,
                        "provider": provider,
                        "model": "new-member",
                        "planner_model": "new-planner",
                        "judge_model": "new-judge",
                    }
                )
                assert (
                    cfg.harness == harness
                    and cfg.model == "new-member"
                    and cfg.judge_model == "new-judge"
                )
                assert cfg.provider == ("vllm" if provider == "local" else provider)
        with pytest.raises(ValidationError):
            Config.model_validate(
                {**spec, "base_url": "https://user:secret@example.com/v1"}
            )


def test_spreadsheet_exposes_shared_strings_alongside_cell_values(tmp_path):
    from zipfile import ZipFile

    from service.materials import searchable

    workbook = tmp_path / "metrics.xlsx"
    with ZipFile(workbook, "w") as book:
        book.writestr("xl/sharedStrings.xml", "<sst><si><t>Revenue</t></si></sst>")
        book.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>321</v></c></row></worksheet>',
        )
    assert "A1=Revenue | B1=321" in searchable(workbook)


def test_worker_quota_uses_original_openai_usage_and_native_output(
    tmp_path, monkeypatch
):
    import httpx
    import yaml

    from run import Config
    from scenarios import ROOT
    from service import budget
    from service.gateway import Gateway

    cfg = Config.model_validate(
        yaml.safe_load((ROOT / "experiments/marble-api.example.yaml").read_text())
    )
    assert (cfg.seed, cfg.max_turns, cfg.max_agent_tokens) == (7, 40, 500000)
    assert cfg.max_team_tokens is None and cfg.max_output_tokens is None
    limits = []

    def handler(self, request):
        body = json.loads(request.content)
        assert "max_tokens" not in body
        limits.append(body["max_completion_tokens"])
        return httpx.Response(
            200,
            json={
                "model": cfg.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 2,
                    "total_tokens": 102,
                    "prompt_tokens_details": {"cached_tokens": 95},
                },
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handler)
    store = Store(tmp_path)
    try:
        gw = Gateway(cfg, store)
        with store.transaction() as db:
            budget.record(db, "A", "worker", 499990)
            budget.record(db, "planner", "planner", 999999)

        def ask(who):
            return gw.forward(
                who,
                "worker",
                "/v1/messages",
                {"messages": [{"role": "user", "content": "work"}], "max_tokens": 8192},
            )[0]

        assert ask("A") == 200
        with pytest.raises(budget.Exhausted):
            ask("A")
        with pytest.raises(budget.Exhausted):
            ask("A")
        assert ask("B") == 200
        assert gw.complete("plan") == "ok"
        assert limits == [10, 3, 8192, 8192]
        with store.transaction() as db:
            assert budget.agent_used(db, "A") == 500004
            assert budget.agent_used(db, "B") == 7
    finally:
        store.close()


def test_cache_accounting_stream_and_missing_usage():
    from service.gateway import token_counts

    messages = b'data: {"message":{"usage":{"input_tokens":11,"cache_creation_input_tokens":4,"cache_read_input_tokens":80,"output_tokens":1}}}\n\ndata: {"usage":{"output_tokens":5}}\n'
    assert token_counts(messages) == (100, 80)
    chat = b'data: {"usage":{"total_tokens":124,"prompt_tokens_details":{"cached_tokens":113}}}\n\ndata: [DONE]\n'
    assert token_counts(chat) == (124, 113)
    assert token_counts(b'{"usage":{"total_tokens":82}}') == (82, 0)
    assert token_counts(b"{}") is None


def test_vllm_worker_cache_hits_and_uncapped_native_output(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import httpx

    from service import budget
    from service.gateway import Gateway

    cfg = SimpleNamespace(
        provider="vllm",
        model="member",
        base_url="http://host/v1",
        api_key_env=None,
        max_team_tokens=None,
        max_agent_tokens=500000,
        max_output_tokens=None,
    )
    limits = []

    def handler(self, request):
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 400000})
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": "member", "max_model_len": 600000}]}
            )
        assert request.url.path == "/v1/messages"
        limits.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(
            200,
            json={
                "usage": {
                    "input_tokens": 4,
                    "cache_read_input_tokens": 399996,
                    "output_tokens": 10,
                }
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handler)
    store = Store(tmp_path)
    try:
        with store.transaction() as db:
            budget.record(db, "A", "worker", 499900)
        gw = Gateway(cfg, store)
        for actor in ("A", "B"):
            assert (
                gw.forward(
                    actor,
                    "worker",
                    "/v1/messages",
                    {
                        "messages": [{"role": "user", "content": "work"}],
                        "max_tokens": 8192,
                    },
                )[0]
                == 200
            )
        assert limits == [100, 8192]
        with store.transaction() as db:
            assert budget.agent_used(db, "A") == 499914
            assert budget.agent_used(db, "B") == 14
    finally:
        store.close()


@pytest.mark.parametrize("provider", ["vllm", "openai"])
def test_codex_responses_uses_worker_budget(tmp_path, monkeypatch, provider):
    import httpx
    from contextlib import closing

    from run import Config
    from service import budget
    from service.gateway import Gateway
    from tests.fakes import responses_stream

    cfg = Config(
        benchmark="marble", task_id="research/2", harness="codex", model="fixture",
        provider=provider, base_url="http://fixture.invalid/v1", api_key="fixture-key",
        planner_mode="fixed", protocol="point_to_point", seed=7, max_turns=40,
        max_output_tokens=30, max_agent_tokens=20, judge_model="judge",
    )
    requests = []

    def serve(transport, req):
        body = json.loads(req.content)
        assert req.url.path == "/v1/responses"
        assert req.headers["authorization"] == "Bearer fixture-key"
        assert body["model"] == "fixture" and body["store"] is False
        requests.append(body["max_output_tokens"])
        return httpx.Response(200, content=responses_stream("ready"), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve)
    payload = {"model": "other", "input": "work", "stream": True}
    with closing(Store(tmp_path)) as store:
        gw = Gateway(cfg, store)
        with pytest.raises(ValueError, match="容器中执行"):
            gw.forward("A", "worker", "/v1/responses", {**payload, "tools": [{"type": "web_search"}]})
        with pytest.raises(PermissionError):
            gw.forward("A", "worker", "/v1/chat/completions", payload)
        assert gw.forward("A", "worker", "/v1/responses", payload)[0] == 200
        with pytest.raises(budget.Exhausted):
            gw.forward("A", "worker", "/v1/responses", payload)
        assert gw.forward("B", "worker", "/v1/responses", payload)[0] == 200
        assert requests == [20, 9, 20]
        with store.transaction() as db:
            assert budget.agent_used(db, "A") == 22
            assert budget.agent_used(db, "B") == 11


def test_marble_messages_reject_provider_web_tools():
    from service.gateway import validate_marble_messages

    validate_marble_messages({"tools": [{"name": "Bash", "input_schema": {}}]})
    with pytest.raises(ValueError, match="场景工具"):
        validate_marble_messages({"tools": [{"type": "web_search_20250305", "name": "web_search"}]})
    with pytest.raises(ValueError, match="远程文件"):
        validate_marble_messages({"messages": [{"content": [
            {"type": "image", "source": {"type": "url", "url": "https://example.org/"}}
        ]}]})


def test_member_effort_defaults_and_native_mismatch():
    from types import SimpleNamespace

    from service.gateway import member_wire_effort

    cfg = SimpleNamespace(harness="codex", provider="openai", member_reasoning_effort=None)
    body = {"reasoning": {"effort": "medium"}}
    assert member_wire_effort(cfg, body, responses=True) == "medium"
    cfg.member_reasoning_effort = "high"
    with pytest.raises(ValueError, match="medium.*high"):
        member_wire_effort(cfg, body, responses=True)

    cfg = SimpleNamespace(harness="deepseek", provider="deepseek", member_reasoning_effort="off")
    body = {"thinking": {"type": "disabled"}}
    assert member_wire_effort(cfg, body, responses=False) == "off"
    cfg.provider = "openai"
    assert member_wire_effort(cfg, body, responses=False) == "none"
    cfg.member_reasoning_effort = None
    assert member_wire_effort(cfg, body, responses=False) is None
