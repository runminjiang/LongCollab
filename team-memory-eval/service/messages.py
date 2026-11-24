import json

from service import archive
from service.events import emit
from service.store import encode


def send(db, sender, target, text, files=(), *, display=None, turn_id=None, **fields):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("消息正文为空")
    archive.grant(db, sender, target, files)
    body = {"from": display or sender, "message": text, "files": list(files), **fields}
    seq = db.execute(
        "INSERT INTO mail(sender,target,body) VALUES(?,?,?)",
        (sender, target, encode(body)),
    ).lastrowid
    emit(
        db,
        "sent",
        sender=sender,
        recipient=target,
        message_id=seq,
        turn_id=turn_id,
        **body,
    )
    return seq


def inbox(db, target):
    mail = []
    for row in db.execute(
        "SELECT id,body FROM mail WHERE target=? AND consumed=0 ORDER BY id", (target,)
    ):
        body = json.loads(row["body"])
        if "message" not in body:
            body["message"] = body.pop("text")
        mail.append({"id": row["id"], **body})
    return mail


def consume(db, target, ids):
    db.executemany(
        "UPDATE mail SET consumed=1 WHERE target=? AND id=?", [(target, i) for i in ids]
    )


def pending(db):
    return db.execute(
        "SELECT target FROM mail WHERE consumed=0 ORDER BY id LIMIT 1"
    ).fetchone()
