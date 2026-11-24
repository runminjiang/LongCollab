"""只投影两个原生 SDK 日志里的可见消息、工具动作与结果。"""

import hashlib
import json
import os
import stat
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path


def blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    items = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text":
            items.append({"type": kind, "text": b.get("text", "")})
        elif kind == "tool_use":
            items.append(
                {
                    "type": "tool",
                    "name": b.get("name"),
                    "input": b.get("input"),
                    "id": b.get("id"),
                }
            )
        elif kind == "tool_result":
            items.append(
                {
                    "type": kind,
                    "id": b.get("tool_use_id"),
                    "content": blocks(b.get("content")),
                    "error": b.get("is_error", False),
                }
            )
        elif kind in {"image", "document"}:
            items.append(
                {"type": kind, "media_type": (b.get("source") or {}).get("media_type")}
            )
    return items


def visible(row, family):
    kind = row.get("type")
    if family == "claude":
        if kind not in {"user", "assistant"}:
            return None
        msg = row.get("message", {})
        return row.get("timestamp"), msg.get("role", kind), blocks(msg.get("content"))

    data = row.get("data", {})
    time = row.get("time")
    if not isinstance(time, (float, int)):
        return None
    when = datetime.fromtimestamp(time / 1000, UTC).isoformat()
    if kind == "tool/call":
        return (
            when,
            "assistant",
            [
                {
                    "type": "tool",
                    "name": data.get("name"),
                    "id": data.get("callId"),
                    "input": data.get("arguments"),
                }
            ],
        )
    if kind in {"system/message", "user/message", "assistant/message", "tool/result"}:
        msg = data.get("message", data)
        return when, msg.get("role", kind), blocks(msg.get("content"))
    return None


def open_log(root, relative):
    # 成员可在自己的挂载下创建链接，打开每一级目录时都拒绝跟随链接。
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root, flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(
            relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise FileNotFoundError
        return os.fdopen(fd, "rb")
    finally:
        os.close(directory)


def native_history(root, member, previous):
    if not member or member in {".", ".."} or Path(member).name != member:
        raise ValueError("成员 ID 无效")
    patterns = {
        "claude": "claude/projects/*/*.jsonl",
        "deepseek": "dsh/sessions/*/*/session.v4.jsonl",
    }
    logs = []
    version = hashlib.sha256()
    with ExitStack() as stack:
        home = root / "members" / member / "home"
        for family, pattern in patterns.items():
            for path in sorted(home.glob(pattern)):
                relative = path.relative_to(root)
                try:
                    stream = stack.enter_context(open_log(root, relative))
                except OSError:
                    continue
                info = os.fstat(stream.fileno())
                version.update(
                    f"{relative}/{info.st_size}/{info.st_mtime_ns}".encode()
                )
                session = path.stem if family == "claude" else path.parent.name
                logs.append((family, session, stream))
        token = version.hexdigest()
        if token == previous:
            return {"version": token}
        records = []
        for family, session, stream in logs:
            for line in stream:
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue  # 正在追加的最后一行会在下一次刷新时读到。
                item = visible(row, family) if isinstance(row, dict) else None
                if item and item[2]:
                    records.append(
                        {
                            "time": item[0],
                            "role": item[1],
                            "content": item[2],
                            "session": session,
                        }
                    )
        records.sort(key=lambda r: r["time"] or "")
        return {"version": token, "records": records}
