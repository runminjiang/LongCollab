"""在成员自己的目录创建或恢复 Codex CLI 会话。"""

import json
import os
import subprocess
from pathlib import Path


def turn(member, payload):
    Path("/state/codex").mkdir(exist_ok=True)
    provider = (
        f'model_providers.tme={{name="TME",base_url="{payload["url"]}/v1",'
        'wire_api="responses",supports_websockets=false}'
    )
    cmd = [
        "codex", "--no-daemon", "--ask-for-approval", "never",
        "-c", 'model_provider="tme"', "-c", provider, "-c", 'web_search="disabled"',
        "-c", "agents.enabled=false",
        "--disable", "multi_agent", "--disable", "multi_agent_v2",
        "exec", "--sandbox", "danger-full-access", "--cd", "/work",
    ]
    session_id = member["session_id"]
    if effort := payload.get("member_reasoning_effort"):
        cmd.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
    if session_id:
        cmd.append("resume")
    cmd.extend([
        "--json", "--ignore-user-config", "--skip-git-repo-check", "--model", payload["model"]
    ])
    if session_id:
        cmd.append(session_id)
    proc = subprocess.run(
        [*cmd, "-"], input=json.dumps(payload["input"], ensure_ascii=False),
        capture_output=True, text=True,
        env={**os.environ, "CODEX_HOME": "/state/codex", "TOKIO_WORKER_THREADS": "4"},
    )
    thread_id = answer = usage = None
    error = proc.stderr.strip()[-1000:]
    for line in proc.stdout.splitlines():
        event = json.loads(line)
        if event["type"] == "thread.started":
            thread_id = event["thread_id"]
        elif event["type"] == "item.completed" and event["item"]["type"] == "agent_message":
            answer = event["item"]["text"]
        elif event["type"] == "turn.completed":
            usage = event["usage"]
        elif event["type"] == "turn.failed":
            error = event["error"]["message"]
    if proc.returncode or not thread_id or not answer or usage is None:
        raise RuntimeError(f"Codex 未完成回合（退出码 {proc.returncode}）：{error}")
    return {
        "session_id": thread_id,
        "text": answer,
        "reason": "completed",
        "native_usage": usage,
    }
