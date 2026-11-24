"""附件都是具名快照。权限和交付版本随回合事务提交。"""

import json
from pathlib import PurePath
from uuid import uuid4

from service.events import emit

LIMIT = 16 * 1024 * 1024


def upload(db, actor, name, data):
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or PurePath(name).name != name
        or len(data) > LIMIT
    ):
        raise ValueError("附件名称或大小无效")
    key = uuid4().hex
    db.execute("INSERT INTO blobs VALUES(?,?,?)", (key, name, data))
    db.execute("INSERT INTO grants VALUES(?,?)", (key, actor))
    emit(db, "uploaded", actor=actor, file=key, name=name)
    return key


def read(db, actor, key):
    row = db.execute(
        "SELECT blobs.* FROM blobs JOIN grants ON grants.blob=blobs.id WHERE actor=? AND id=?",
        (actor, key),
    ).fetchone()
    if row is None:
        raise ValueError("附件不存在或未获授权")
    return row


def grant(db, sender, target, files):
    for key in files:
        read(db, sender, key)
        db.execute("INSERT OR IGNORE INTO grants VALUES(?,?)", (key, target))


def validate(data, form):
    text = data.decode("utf-8")
    if not text.strip():
        raise ValueError("交付内容为空")
    if form == "report_json":
        report = json.loads(text)
        if (
            not isinstance(report, dict)
            or not isinstance(report.get("report_text"), str)
            or not report["report_text"].strip()
        ):
            raise ValueError("缺少 report_text")
        items = report.get("report_insights")
        if not isinstance(items, list) or any(
            not isinstance(i, dict)
            or not isinstance(i.get("claim"), str)
            or not i["claim"].strip()
            or not isinstance(i.get("citations"), list)
            or not all(isinstance(c, str) for c in i["citations"])
            for i in items
        ):
            raise ValueError("report_insights 必须包含 claim 和 citations")
    elif form != "text":
        raise ValueError(f"未知交付格式 {form}")


def submit(db, task, actor, slot, key, turn):
    if slot not in task.outputs or task.owners.get(slot, actor) != actor:
        raise ValueError("交付项不存在或提交者无权交付")
    blob = read(db, actor, key)
    validate(blob["data"], task.outputs[slot])
    db.execute(
        "INSERT INTO submissions(slot,actor,blob,turn) VALUES(?,?,?,?)",
        (slot, actor, key, turn),
    )
    emit(db, "submitted", slot=slot, actor=actor, file=key, turn_id=turn)


def latest(db):
    rows = db.execute(
        "SELECT slot,actor,blob,version FROM submissions ORDER BY version"
    )
    return {r["slot"]: dict(r) for r in rows}
