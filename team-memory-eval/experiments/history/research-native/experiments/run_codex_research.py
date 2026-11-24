"""用独立的 Codex CLI 会话运行指定的 MARBLE Research 题目。"""

import argparse
import json
import os
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scenarios import load  # noqa: E402
from scenarios.marble import research_environment  # noqa: E402
from service.gateway import api_base, validate_responses_request  # noqa: E402
from service.store import write_json  # noqa: E402

IMAGE = "tme-claude-code:b1"
OPENAI_ENDPOINT = "https://api.openai.com/v1"
CHATGPT_ENDPOINT = "https://chatgpt.com/backend-api/codex"
PIDS_LIMIT = 512
TOKIO_WORKER_THREADS = 4


class HostServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def chatgpt_headers(client_version):
    auth_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        if auth["auth_mode"] != "chatgpt":
            raise ValueError
        token = auth["tokens"]["access_token"]
        account = auth["tokens"]["account_id"]
        if not all(
            isinstance(value, str) and value and value.isascii()
            and value.isprintable() and " " not in value
            for value in (token, account)
        ):
            raise ValueError
    except (OSError, TypeError, KeyError, ValueError):
        raise ValueError("未读取到宿主 Codex 的文件版 ChatGPT 登录，请检查 codex login") from None
    return {
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-Id": account,
        "User-Agent": f"codex_cli_rs/{client_version}",
        "originator": "codex_cli_rs",
    }


def research_catalog(task):
    descriptions = research_environment({}).action_handler_descriptions
    return [
        {
            "name": name,
            "description": descriptions[name]["function"]["description"],
            "inputSchema": descriptions[name]["function"]["parameters"],
        }
        for name in task.operations
    ]


def request_handler(task, catalog, endpoint, auth_headers, model, directory):
    log_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 24 * 1024 * 1024:
                    raise ValueError("请求长度无效")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求需要为 JSON 对象")
                if self.path == "/catalog":
                    self.reply(200, {"tools": catalog})
                elif self.path == "/tool":
                    self.tool(body)
                elif self.path in {"/v1/responses", "/v1/responses/compact"}:
                    validate_responses_request(body, model)
                    self.forward(body)
                else:
                    self.reply(404, {"error": "路径未开放"})
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                self.reply(400, {"error": str(error)})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def tool(self, body):
            name = body["name"]
            if name not in task.operations:
                raise ValueError("工具未开放")
            args = body.get("arguments", {})
            if not isinstance(args, dict):
                raise ValueError("工具参数需要为对象")
            _, operation = task.operations[name]
            try:
                value = operation("codex", args)
            except Exception as error:
                # 原版工具的外部服务异常作为本次工具结果返回。
                value = {"success": False, "error-msg": f"{type(error).__name__}: {error}"}
            with log_lock, (directory / "tools.jsonl").open("a", encoding="utf-8") as log:
                print(
                    json.dumps(
                        {"name": name, "arguments": args, "result": value},
                        ensure_ascii=False,
                    ),
                    file=log,
                )
            self.reply(
                200,
                {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                    "isError": not value.get("success", False),
                },
            )

        def forward(self, body):
            try:
                headers = auth_headers()
            except ValueError as error:
                self.reply(401, {"error": str(error)})
                return
            if self.path == "/v1/responses":
                body["store"] = False
            started = False
            try:
                with httpx.stream(
                    "POST", endpoint + self.path.removeprefix("/v1"),
                    headers=headers, json=body, timeout=180, follow_redirects=False,
                ) as response:
                    if response.status_code in {401, 403}:
                        error = (
                            "ChatGPT 拒绝宿主登录或模型请求，请检查 codex login 和此模型的访问权限"
                            if endpoint == CHATGPT_ENDPOINT
                            else "模型端点拒绝宿主凭据或模型请求"
                        )
                        self.reply(response.status_code, {"error": error})
                        return
                    self.send_response(response.status_code)
                    self.send_header(
                        "Content-Type", response.headers.get("Content-Type", "application/json")
                    )
                    self.end_headers()
                    started = True
                    for data in response.iter_bytes():
                        self.wfile.write(data)
            except httpx.HTTPError as error:
                if not started:
                    self.reply(502, {"error": f"模型连接失败：{type(error).__name__}"})
                else:
                    self.close_connection = True

    return Handler


def prompt_for(task):
    roles = "\n\n".join(role["id"] + "\n" + role["description"] for role in task.roles)
    return (
        "If native subagents are available, assign one to each researcher profile "
        "and synthesize their findings. "
        "Otherwise analyze the task from each profile's perspective. "
        "Use the MARBLE MCP tools for literature search and access to research sources. "
        "Submit one research proposal in English.\n\n"
        f"Research task\n{task.goal}\n\nResearcher profiles\n{roles}\n\n"
        f"Required output\n{task.format}\n"
    )


def run_task(task, directory, args, codex, metadata, catalog, auth_headers):
    directory.mkdir()
    manifest = {**metadata, "task_id": task.id, "revision": task.revision, "status": "running"}
    write_json(directory / "run.json", manifest)
    (directory / "prompt.txt").write_text(prompt_for(task), encoding="utf-8")
    worker = Path(__file__).with_name("codex_research_worker.py").resolve()
    name = "tme-codex-research-" + uuid4().hex
    uid, gid = os.getuid(), os.getgid()
    with TemporaryDirectory(prefix="tme-codex-research-", dir="/tmp") as scratch:
        socket_path = Path(scratch) / "host.sock"
        handler = request_handler(task, catalog, args.endpoint, auth_headers, args.model, directory)
        with HostServer(str(socket_path), handler) as server:
            socket_path.chmod(0o600)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            command = [
                "docker", "run", "--rm", "--pull=never", "-i", "--name", name,
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", str(PIDS_LIMIT),
                "--user", f"{uid}:{gid}",
                "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size=512m,uid={uid},gid={gid}",
                "--tmpfs", f"/state:rw,nosuid,nodev,size=256m,uid={uid},gid={gid}",
                "--tmpfs", f"/work:rw,nosuid,nodev,size=256m,uid={uid},gid={gid}",
                "--workdir", "/work",
                "-e", "HOME=/state", "-e", "CODEX_HOME=/state/codex",
                "-e", f"TOKIO_WORKER_THREADS={TOKIO_WORKER_THREADS}",
                "-e", "NO_PROXY=127.0.0.1,localhost", "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-v", f"{codex.parent}:/opt/codex:ro", "-v", f"{worker}:/opt/worker.py:ro",
                "-v", f"{socket_path}:/tme/host.sock:ro",
                "--entrypoint", "python", metadata["image_id"], "/opt/worker.py",
                args.model, args.effort, args.mode,
            ]
            start = datetime.now(UTC)
            try:
                with (
                    (directory / "prompt.txt").open("rb") as source,
                    (directory / "events.jsonl").open("wb") as events,
                    (directory / "stderr.log").open("wb") as errors,
                ):
                    process = subprocess.run(
                        command, stdin=source, stdout=events, stderr=errors,
                        timeout=args.timeout, check=False,
                    )
                manifest["exit_code"] = process.returncode
                if process.returncode:
                    raise RuntimeError(f"Codex 未完成交付，退出码 {process.returncode}")
                with (directory / "events.jsonl").open(encoding="utf-8") as events:
                    records = [json.loads(line) for line in events if line.strip()]
                session_ids = [r["thread_id"] for r in records if r["type"] == "thread.started"]
                if len(session_ids) != 1 or records[-1]["type"] != "research.delivered":
                    raise RuntimeError("Codex 未记录唯一会话或最终交付")
                manifest["session_id"] = session_ids[0]
                answer = records[-1]["answer"]
                (directory / "idea").write_text(answer, encoding="utf-8")
                result = {
                    "scenario": "research",
                    "task_id": int(task.id.split("/")[1]),
                    "answer": answer,
                }
                (directory / "answer.jsonl").write_text(
                    json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                manifest["status"] = "completed"
            except (
                OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt
            ) as error:
                manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
                subprocess.run(
                    ["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=15, check=False,
                )
                raise
            finally:
                server.shutdown()
                thread.join()
                manifest["elapsed_seconds"] = (datetime.now(UTC) - start).total_seconds()
                write_json(directory / "run.json", manifest)
    print(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, nargs="+", required=True, metavar="N",
                        help="明确指定题号，例如 --tasks 1 3")
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--mode", choices=("native", "single"), default="native")
    parser.add_argument("--auth", choices=("api-key", "chatgpt"), default="api-key",
                        help="用 API key 或复用宿主的 Codex ChatGPT 登录")
    parser.add_argument("--endpoint", help=f"Responses 端点，默认 {OPENAI_ENDPOINT}")
    parser.add_argument("--api-key-env", help="API key 的环境变量名，默认 OPENAI_API_KEY")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if any(n < 1 for n in args.tasks) or len(set(args.tasks)) != len(args.tasks):
        parser.error("--tasks 需要互不重复的正整数")
    if args.timeout < 1:
        parser.error("--timeout 需要为正整数")
    if args.auth == "chatgpt" and (args.endpoint is not None or args.api_key_env is not None):
        parser.error("--auth chatgpt 仅使用官方 Codex 端点，不能指定 --endpoint 或 --api-key-env")

    codex = Path(shutil.which("codex") or "codex").resolve(strict=True)
    codex_version = subprocess.check_output([codex, "--version"], text=True, timeout=15).strip()

    try:
        if args.auth == "chatgpt":
            args.endpoint = CHATGPT_ENDPOINT
            auth_headers = partial(chatgpt_headers, codex_version.removeprefix("codex-cli "))
            auth_headers()
        else:
            args.endpoint = api_base(args.endpoint or OPENAI_ENDPOINT)
            key_var = args.api_key_env or "OPENAI_API_KEY"
            key = os.environ.get(key_var)
            if not key and args.endpoint == OPENAI_ENDPOINT:
                parser.error(f"宿主未设置 {key_var}")

            def auth_headers():
                return {"Authorization": f"Bearer {key}"} if key else {}

    except ValueError as error:
        parser.error(str(error))
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    try:
        tasks = [load("marble", f"research/{n}") for n in args.tasks]
    except ValueError as error:
        parser.error(str(error))
    catalog = research_catalog(tasks[0])
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE], text=True, timeout=15
    ).strip()
    metadata = {
        "codex": codex_version,
        "image_id": image_id, "model": args.model, "effort": args.effort,
        "mode": args.mode, "multi_agent_version": "v1" if args.mode == "native" else None,
        "retrieval": "marble_research", "tools": list(tasks[0].operations),
        "timeout_seconds": args.timeout, "model_auth": args.auth,
        "container_pids_limit": PIDS_LIMIT, "tokio_worker_threads": TOKIO_WORKER_THREADS,
    }
    args.output.mkdir(parents=True, exist_ok=False)
    failed = False
    for n, task in zip(args.tasks, tasks):
        try:
            run_task(task, args.output / f"research-{n}", args, codex, metadata, catalog, auth_headers)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            print(f"{task.id} {type(error).__name__}: {error}", file=sys.stderr)
            failed = True
    return int(failed)


def handle_terminate(*_):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_terminate)
    raise SystemExit(main())
