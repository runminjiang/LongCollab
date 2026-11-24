import asyncio
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from runtime.envs.team import Member
from runtime.sandbox import Sandbox
from service.api import TeamService
from service.store import Store
from tests.fakes import sample_task
from tools import client


async def test_slow_lookup_can_return_a_handoff(tmp_path, monkeypatch):
    loop = asyncio.get_running_loop()
    clock = loop.time
    config = SimpleNamespace(harness="deepseek", model="test")
    member = Member("A", "A", "检索论文", ["public"])
    proc = Mock(returncode=0, wait=AsyncMock())
    connection = Mock(wraps=client.UnixHTTP)
    monkeypatch.setattr(client, "UnixHTTP", connection)

    def lookup(actor, args):
        # 让宿主在检索期间跨过旧的 360 秒截止时间。
        loop.call_soon_threadsafe(
            monkeypatch.setattr, loop, "time", lambda: clock() + 417
        )
        return {"papers": ["论文"]}

    async def communicate(raw):
        monkeypatch.setenv("TME_TURN_TOKEN", json.loads(raw)["token"])
        status, _, body = await asyncio.to_thread(
            client.request, "/team", {"op": "get_recent_papers", "args": {}}
        )
        assert status == 200
        paper = json.loads(body)["papers"][0]
        await asyncio.sleep(0.01)
        action = {"kind": "handoff", "to": "B", "message": paper}
        reply = {"ok": True, "output": {"text": json.dumps(action)}}
        return json.dumps(reply).encode(), b""

    task = sample_task()
    task.operations["get_recent_papers"] = ("public", lookup)
    proc.communicate = communicate
    monkeypatch.setattr(
        "runtime.sandbox.subprocess.run",
        Mock(return_value=Mock(returncode=0, stdout="image")),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    with closing(Store(tmp_path)) as store, TeamService(task, store, None) as service:
        monkeypatch.setenv("TME_SOCKET", str(service.socket))
        sandbox = Sandbox(tmp_path, config, service)
        result = await sandbox.turn(member, {}, "turn")
        assert result["action"] == {"kind": "handoff", "to": "B", "message": "论文"}
        connection.assert_called_once_with("localhost", timeout=None)


def test_scoped_tokens_and_hidden_inputs(tmp_path):
    task = sample_task()
    task.reference = {"secret": "judge-only"}
    path = tmp_path / "doc"
    path.write_text("private")
    task.materials = {"own": ("public", path), "other": ("locked", path)}
    assert "judge-only" not in json.dumps(task.public())
    s = Store(tmp_path)
    try:
        with TeamService(task, s, None) as api:
            with api.lease(Member("A", "A", "read", ["public"])) as token:

                def read(op, **args):
                    return json.loads(
                        api.request(token, "/team", {"op": op, "args": args})[2]
                    )

                assert read("list")["files"] == ["own"]
                assert read("read", name="own")["text"] == "private"
                with pytest.raises(KeyError):
                    read("read", name="other")
                with pytest.raises(KeyError):
                    read("read", name="../doc")
                with pytest.raises(RuntimeError), api.lease(Member("B", "B", "", [])):
                    pass
            with pytest.raises(PermissionError):
                api.request(token, "/team", {"op": "list"})
    finally:
        s.close()


def test_marble_web_checks_redirect_and_pins_public_address(monkeypatch):
    import socket

    import requests

    from scenarios import marble_web

    resolved = {"paper.example": "93.184.216.34", "blocked.example": "127.0.0.1"}
    def resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved[host], 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    contacted = []
    def send(adapter, request, **kwargs):
        assert not kwargs["proxies"]
        host, tls = adapter.build_connection_pool_key_attributes(request, True)
        contacted.append((host["host"], request.headers["Host"], tls["server_hostname"]))
        response = requests.Response()
        response.status_code = 302
        response.url = request.url
        response.request = request
        response._content = b""
        response.headers["Location"] = "https://blocked.example/paper"
        return response

    monkeypatch.setattr(marble_web.PublicWebAdapter, "send", send)
    with pytest.raises(requests.RequestException, match="公网"):
        marble_web.get("https://paper.example/paper")
    assert contacted == [("93.184.216.34", "paper.example", "paper.example")]


def test_codex_chatgpt_login_stays_at_host_gateway(tmp_path, monkeypatch):
    from threading import Thread

    import httpx

    from experiments.run_codex_research import (
        CHATGPT_ENDPOINT,
        HostServer,
        chatgpt_headers,
        request_handler,
    )

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("TME_SOCKET", str(tmp_path / "host.sock"))
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "fixture-token", "account_id": "fixture-account"},
    }), encoding="utf-8")
    requests = []

    def upstream(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, text="data: ok\n\n")
        return httpx.Response(401, text="fixture-token")

    def auth_headers():
        return chatgpt_headers("0.166.0")

    handler = request_handler(sample_task(), [], CHATGPT_ENDPOINT, auth_headers, "fixture", tmp_path)
    with (
        httpx.Client(transport=httpx.MockTransport(upstream)) as model,
        HostServer(str(tmp_path / "host.sock"), handler) as server,
    ):
        monkeypatch.setattr(httpx, "stream", model.stream)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            def call(path, **body):
                with closing(client.UnixHTTP("localhost", timeout=5)) as conn:
                    conn.request(
                        "POST", path, json.dumps(body), {"Authorization": "Bearer container"}
                    )
                    response = conn.getresponse()
                    return response.status, response.read()

            assert call("/v1/responses", model="fixture", store=True) == (200, b"data: ok\n\n")
            req = requests[0]
            assert str(req.url) == CHATGPT_ENDPOINT + "/responses"
            assert req.headers["Authorization"] == "Bearer fixture-token"
            assert req.headers["ChatGPT-Account-Id"] == "fixture-account"
            assert req.headers["User-Agent"] == "codex_cli_rs/0.166.0"
            assert json.loads(req.content) == {"model": "fixture", "store": False}

            status, body = call("/v1/responses", model="fixture")
            assert status == 401 and b"fixture-token" not in body
            assert call(
                "/v1/responses", model="fixture", tools=[{"type": "web_search"}]
            )[0] == 400
            assert call("/auth.json")[0] == 404
            auth_file.unlink()
            assert call("/v1/responses", model="fixture")[0] == 401
            assert len(requests) == 2
        finally:
            server.shutdown()
            thread.join()
