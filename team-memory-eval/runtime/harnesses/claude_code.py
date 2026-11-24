import asyncio
import json

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query


async def _run(member, payload):
    tools = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
    opts = ClaudeAgentOptions(
        cwd="/work",
        model=payload["model"],
        effort=payload.get("member_reasoning_effort"),
        resume=member["session_id"],
        setting_sources=[],
        tools=tools,
        allowed_tools=tools,
        disallowed_tools=["Agent", "Task", "Skill", "WebFetch", "WebSearch"],
        strict_mcp_config=True,
        mcp_servers={},
        permission_mode="acceptEdits",
        env={
            "ANTHROPIC_BASE_URL": payload["url"],
            "ANTHROPIC_API_KEY": "tme",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CONFIG_DIR": "/state/claude",
        },
    )
    async for msg in query(
        prompt=json.dumps(payload["input"], ensure_ascii=False), options=opts
    ):
        if isinstance(msg, ResultMessage):
            if msg.is_error:
                raise RuntimeError(f"Claude {msg.subtype}: {msg.result or msg.errors}")
            return {
                "session_id": msg.session_id,
                "text": msg.result,
                "reason": msg.stop_reason,
                "native_usage": msg.usage,
                "native_usd": msg.total_cost_usd,
            }
    raise RuntimeError("Claude 未返回 ResultMessage")


def turn(member, payload):
    return asyncio.run(_run(member, payload))
