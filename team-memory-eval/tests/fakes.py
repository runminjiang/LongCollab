"""脚本化成员和两种 HTTP 模型协议，用同一套场景验证原生 SDK。"""

import base64
import json
import threading
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from scenarios import Task


def sample_task(n=2):
    ids = list("ABCD"[:n])
    return Task(
        "marble",
        "fixture",
        "fixture",
        "Write the idea.",
        "plain text",
        [{"id": i, "role": "role " + i, "resource_ids": ["public"]} for i in ids],
        {"public": "documents"},
        {"idea": "text"},
    )


def fixed(task):
    return {
        "members": [
            {
                "id": r["id"],
                "role": r["role"],
                "resource_ids": r["resource_ids"],
                "assignment": "Work as " + r["role"],
            }
            for r in task.roles
        ],
        "first_member": task.roles[0]["id"],
    }


class ScriptedSandbox:
    def __init__(self, directory, config, service, scripts):
        self.service = service
        self.scripts = {k: deque(v) for k, v in scripts.items()}
        self.seen, self.sessions = [], {}

    def prepare(self, member):
        pass

    async def turn(self, member, prompt, turn):
        self.seen.append((member.id, prompt, member.session_id))
        action = self.scripts[member.id].popleft()
        with self.service.lease(member) as token:
            if callable(action):
                action = action(self.service, token, prompt)
        sid = self.sessions.setdefault(member.id, uuid4().hex)
        return {"session_id": sid, "action": action, "tokens": 3}


def submit(service, token, data=b"result", slot="idea"):
    _, _, raw = service.request(
        token,
        "/team",
        {
            "op": "upload",
            "args": {"name": "draft", "base64": base64.b64encode(data).decode()},
        },
    )
    return {slot: json.loads(raw)["id"]}


def envelope(family, answer, stream, model):
    tool = isinstance(answer, tuple)
    ident = "test_" + uuid4().hex
    if family == "anthropic":
        block = (
            {"type": "tool_use", "id": ident, "name": answer[0], "input": answer[1]}
            if tool
            else {"type": "text", "text": answer}
        )
        reason = "tool_use" if tool else "end_turn"
        msg = {
            "id": "msg_" + ident,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [block],
            "stop_reason": reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 8, "output_tokens": 5},
        }
        if not stream:
            return "application/json", json.dumps(msg).encode()
        start = {
            **msg,
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": 8, "output_tokens": 0},
        }
        first = {**block, "input": {}} if tool else {"type": "text", "text": ""}
        delta = (
            {"type": "input_json_delta", "partial_json": json.dumps(answer[1])}
            if tool
            else {"type": "text_delta", "text": answer}
        )
        frames = [
            {"type": "message_start", "message": start},
            {"type": "content_block_start", "index": 0, "content_block": first},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": reason, "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
        return "text/event-stream", "".join(
            "event: " + f["type"] + "\ndata: " + json.dumps(f) + "\n\n" for f in frames
        ).encode()
    message = {"role": "assistant", "content": None if tool else answer}
    if tool:
        message["tool_calls"] = [
            {
                "id": ident,
                "type": "function",
                "function": {"name": answer[0], "arguments": json.dumps(answer[1])},
            }
        ]
    reason = "tool_calls" if tool else "stop"
    base = {"id": "chatcmpl-" + ident, "created": 1, "model": model}
    usage = {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13}
    if not stream:
        return "application/json", json.dumps(
            {
                **base,
                "object": "chat.completion",
                "usage": usage,
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
            }
        ).encode()
    if tool:
        message["tool_calls"][0]["index"] = 0
    chunks = [
        {
            **base,
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": message, "finish_reason": None}],
        },
        {
            **base,
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
            "usage": usage,
        },
    ]
    return "text/event-stream", (
        "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    ).encode()


@contextmanager
def model_server(respond, *, protocol=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["content-length"])))
            family = "anthropic" if "/messages" in self.path else "openai"
            if self.path.endswith("/responses"):
                family = "responses"
            try:
                if protocol is not None and protocol != family:
                    raise ValueError(f"脚本端点要求 {protocol}，收到 {self.path}")
                if self.path.endswith("count_tokens"):
                    typ, raw = "application/json", b'{"input_tokens":8}'
                else:
                    calls.append(data)
                    answer = respond(data)
                    if family == "responses":
                        typ, raw = "text/event-stream", responses_stream(answer)
                    else:
                        typ, raw = envelope(family, answer, data.get("stream", False), data["model"])
                status = 200
            except Exception as error:  # noqa: BLE001 — 脚本端点也需把断言失败返回客户端
                typ, raw, status = (
                    "application/json",
                    json.dumps({"error": str(error)}).encode(),
                    500,
                )
            self.send_response(status)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            t.join()


def responses_stream(answer):
    ident = uuid4().hex
    if isinstance(answer, tuple):
        namespace, _, name = answer[0].rpartition(".")
        item = {
            "type": "function_call", "id": "fc_" + ident, "call_id": "call_" + ident,
            "name": name, "arguments": json.dumps(answer[1]),
        }
        if namespace:
            item["namespace"] = namespace
    else:
        item = {
            "type": "message", "id": "msg_" + ident, "role": "assistant",
            "content": [{"type": "output_text", "text": answer}],
        }
    response = {
        "id": "resp_" + ident, "object": "response", "status": "completed", "output": [item],
        "usage": {
            "input_tokens": 8, "output_tokens": 5, "total_tokens": 13,
            "input_tokens_details": {"cached_tokens": 2},
        },
    }
    events = [
        {"type": "response.created", "response": {"id": response["id"]}},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]
    return "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()


def offers_delegation(request):
    """Codex 可以在请求顶层或输入的 additional_tools 条目中声明工具。"""
    for group in (request, *request.get("input", [])):
        for tool in group.get("tools", []):
            entries = tool.get("tools", [tool])
            if any(entry.get("name") == "spawn_agent" for entry in entries):
                return True
    return False
