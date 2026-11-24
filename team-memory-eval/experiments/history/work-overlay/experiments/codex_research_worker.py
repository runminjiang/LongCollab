"""断网容器中的 Codex 和标准输入输出 MCP 服务器。"""

import json
import os
import select
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

MCP_TOOL_TIMEOUT = 180


class SocketBridge(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.connect("/tme/host.sock")
            connections = {self.request: peer, peer: self.request}
            while True:
                readable, _, _ = select.select(list(connections), [], [], MCP_TOOL_TIMEOUT + 10)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    connections[source].sendall(data)


def serve_tools(base, benchmark="marble"):
    session = uuid4().hex
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        params = request.get("params", {})
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": benchmark, "version": "1"},
                }
            elif method in {"tools/list", "tools/call"}:
                path = "/catalog" if method == "tools/list" else "/tool"
                if benchmark == "drbench":
                    params = {**params, "tool_session": session}
                with urlopen(
                    Request(
                        base + path,
                        data=json.dumps(params).encode(),
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=MCP_TOOL_TIMEOUT,
                ) as reply:
                    result = json.load(reply)
            elif method == "ping":
                result = {}
            else:
                raise ValueError(f"未知 MCP 方法：{method}")
            response["result"] = result
        except Exception as error:
            response["error"] = {"code": -32603, "message": str(error)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


def run_codex(model, effort, mode, benchmark="marble"):
    Path(os.environ["CODEX_HOME"]).mkdir()
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), SocketBridge) as bridge:
        bridge.daemon_threads = True
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{bridge.server_address[1]}"
        args = [
            "/opt/codex/codex", "--no-daemon", "--ask-for-approval", "never",
            "exec", "--ignore-user-config", "--skip-git-repo-check",
            "--sandbox", "danger-full-access", "--cd", "/work",
            "--json", "--output-last-message", "/work/idea", "--model", model,
            "-c", 'model_provider="tme"',
            "-c", 'web_search="disabled"',
            "-c", f"model_reasoning_effort={json.dumps(effort)}",
            "-c", (
                f'model_providers.tme={{name="TME",base_url="{base}/v1",'
                'wire_api="responses",supports_websockets=false}'
            ),
            "-c", (
                f'mcp_servers.{benchmark}={{command="/usr/local/bin/python",'
                f'args=["/opt/worker.py","mcp","{base}","{benchmark}"],required=true,'
                f'tool_timeout_sec={MCP_TOOL_TIMEOUT}}}'
            ),
            "--enable" if mode == "native" else "--disable", "multi_agent",
            "--disable", "multi_agent_v2",
            "-",
        ]
        try:
            result = subprocess.run(args, check=False)
            if result.returncode:
                return result.returncode
            idea = Path("/work/idea")
            if benchmark == "drbench" and Path("/work/report.json").is_file():
                idea = Path("/work/report.json")
            if not idea.is_file() or idea.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Codex 未交付有效文件")
            answer = idea.read_text(encoding="utf-8")
            if not answer.strip():
                raise ValueError("Codex 交付为空")
            print(
                json.dumps({"type": "research.delivered", "answer": answer}, ensure_ascii=False),
                flush=True,
            )
            return 0
        finally:
            bridge.shutdown()
            thread.join()


if __name__ == "__main__":
    if sys.argv[1] == "mcp":
        serve_tools(*sys.argv[2:])
    else:
        raise SystemExit(run_codex(*sys.argv[1:]))
