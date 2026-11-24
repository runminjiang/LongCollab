"""每次启动处理一个原生回合。SDK 的 HTTP 请求只经本地转接到宿主。"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.client import request


class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            status, typ, raw = request(self.path.split("?")[0], body, self.headers)
        except Exception as error:  # noqa: BLE001 — 把宿主连接故障返回给 SDK
            status, typ, raw = (
                502,
                "application/json",
                json.dumps({"error": str(error)}).encode(),
            )
        self.send_response(status)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    payload = json.load(sys.stdin)
    os.environ["TME_TURN_TOKEN"] = payload.pop("token")
    # DeepSeek 的持久 shell 过滤环境变量。凭证只写入本容器本轮的内存盘。
    token_file = Path("/tmp/tme-turn.token")
    token_file.touch(mode=0o600, exist_ok=False)
    token_file.write_text(os.environ["TME_TURN_TOKEN"])
    if payload["harness"] == "deepseek":
        from . import deepseek as adapter
    elif payload["harness"] == "claude-code":
        from . import claude_code as adapter
    elif payload["harness"] == "codex":
        from . import codex as adapter
    else:
        raise ValueError("未知的成员 harness")
    with ThreadingHTTPServer(("127.0.0.1", 0), Proxy) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            payload["url"] = f"http://127.0.0.1:{proxy.server_port}"
            member = payload["member"]
            output = adapter.turn(member, payload)
            print(json.dumps({"ok": True, "output": output}), flush=True)
        except Exception as error:  # noqa: BLE001 — 将原生成员回合的异常带回宿主
            print(
                json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}),
                flush=True,
            )
        finally:
            proxy.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
