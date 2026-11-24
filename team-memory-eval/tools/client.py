"""成员端只通过当前回合的 Unix socket 访问宿主。"""

import argparse
import base64
import http.client
import json
import os
import socket
from pathlib import Path


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.environ.get("TME_SOCKET", "/tme/service.sock"))


def request(path, payload, headers=None):
    # 基准工具可能要逐篇读取论文，等待宿主返回完整结果。
    connection = UnixHTTP("localhost", timeout=None if path == "/team" else 200)
    try:
        body = json.dumps(payload).encode()
        token_file = Path("/tmp/tme-turn.token")
        token = (
            token_file.read_text()
            if token_file.is_file()
            else os.environ["TME_TURN_TOKEN"]
        )
        h = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
        if headers and headers.get("anthropic-beta"):
            h["anthropic-beta"] = headers["anthropic-beta"]
        connection.request("POST", path, body, h)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "op", choices=["list", "read", "search", "call", "upload", "download"]
    )
    parser.add_argument("args", nargs="*")
    p = parser.parse_args()
    op, a = p.op, p.args
    arity = {
        "list": (0, 0),
        "read": (1, 2),
        "download": (1, 2),
        "call": (2, 2),
        "upload": (1, 1),
        "search": (1, None),
    }
    minimum, maximum = arity[op]
    if len(a) < minimum or (maximum is not None and len(a) > maximum):
        parser.error(
            f"{op} 参数数量无效。用法为 list | read NAME [DEST] | search QUERY | call NAME JSON | upload PATH | download ID [DEST]"
        )
    fields = {}
    if op == "upload":
        file = Path(a[0])
        fields = {
            "name": file.name,
            "base64": base64.b64encode(file.read_bytes()).decode(),
        }
    elif op == "download":
        fields = {"id": a[0]}
    elif op == "call":
        op, fields = a[0], json.loads(a[1])
    elif op == "read":
        fields = {"name": a[0], "binary": len(a) > 1}
    elif op == "search":
        fields = {"query": " ".join(a)}
    status, _, raw = request("/team", {"op": op, "args": fields})
    if status != 200:
        raise SystemExit(raw.decode(errors="replace"))
    result = json.loads(raw)
    if op == "download" or (op == "read" and len(a) > 1):
        dest = Path(a[1] if len(a) > 1 else Path(result["name"]).name)
        dest.write_bytes(base64.b64decode(result["base64"]))
        print(dest)
    else:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
