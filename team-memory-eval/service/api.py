"""每次运行一个私有 Unix socket，单轮身份由宿主绑定。"""

import base64
import hmac
import json
import secrets
import socketserver
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from service import archive, materials
from service.events import emit
from service.store import encode


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = False
    block_on_close = True


class TeamService:
    def __init__(self, task, store, gateway):
        self.task, self.store, self.gateway = task, store, gateway
        self.active = None
        self.guard = threading.Condition()
        self.inflight = 0

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tme-socket-")
        self.socket = Path(self.tmp.name) / "service.sock"
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    length = int(self.headers.get("content-length", 0))
                    if not 0 < length < 24 * 1024 * 1024:
                        raise ValueError("请求长度无效")
                    token = self.headers.get("authorization", "").removeprefix(
                        "Bearer "
                    ) or self.headers.get("x-api-key", "")
                    result = service.request(
                        token,
                        self.path.split("?")[0],
                        json.loads(self.rfile.read(length)),
                        self.headers,
                    )
                    status, content_type, body = result
                except PermissionError as e:
                    status, content_type, body = (
                        403,
                        "application/json",
                        encode({"error": str(e)}).encode(),
                    )
                except (KeyError, ValueError, TypeError) as e:
                    status, content_type, body = (
                        400,
                        "application/json",
                        encode({"error": str(e)}).encode(),
                    )
                except Exception as e:  # noqa: BLE001 — 将场景插件异常返回给发起调用的成员
                    status, content_type, body = (
                        502,
                        "application/json",
                        encode({"error": str(e)}).encode(),
                    )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = Server(str(self.socket), Handler)
        self.socket.chmod(0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    @contextmanager
    def lease(self, member):
        token = secrets.token_urlsafe(32)
        with self.guard:
            if self.active is not None or self.inflight:
                raise RuntimeError("另一个成员仍持有执行权")
            self.active = token, member
        try:
            yield token
        finally:
            with self.guard:
                self.active = None
                self.guard.wait_for(lambda: self.inflight == 0)

    def request(self, token, path, data, headers=None):
        with self.guard:
            if not self.active or not hmac.compare_digest(token, self.active[0]):
                raise PermissionError("无效或已失效的回合凭证")
            member = self.active[1]
            self.inflight += 1
        try:
            if path != "/team":
                return self.gateway.forward(
                    member.id,
                    member.category,
                    path,
                    data,
                    headers,
                )
            return self._team_request(member, data)
        finally:
            with self.guard:
                self.inflight -= 1
                self.guard.notify_all()

    def _team_request(self, member, data):
        op = data["op"]
        args = data.get("args", {})
        if op == "upload":
            content = base64.b64decode(args["base64"], validate=True)
            with self.store.transaction() as db:
                result = {"id": archive.upload(db, member.id, args["name"], content)}
        elif op == "download":
            with self.store.transaction() as db:
                row = archive.read(db, member.id, args["id"])
                emit(
                    db, "downloaded", actor=member.id, file=row["id"], name=row["name"]
                )
                result = {
                    "name": row["name"],
                    "base64": base64.b64encode(row["data"]).decode(),
                }
        else:
            result = materials.access(
                self.task, member.id, member.resource_ids, op, args
            )
            with self.store.transaction() as db:
                # 二进制材料只记名称，避免把整份文件重复写进事件流。
                observed = (
                    {"name": args["name"], "type": "binary"}
                    if op == "read" and args.get("binary")
                    else result
                )
                emit(
                    db,
                    "environment_tool",
                    actor=member.id,
                    op=op,
                    args=args,
                    result=observed,
                )
        return 200, "application/json", encode(result).encode()
