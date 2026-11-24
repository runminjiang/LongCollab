import json
from datetime import UTC, datetime

from service.store import encode


def emit(db, kind, **data):
    timestamp = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO events(kind,data) VALUES(?,?)",
        (kind, encode({"time": timestamp, **data})),
    )


def export(db, path):
    with path.open("w") as stream:
        for row in db.execute("SELECT * FROM events ORDER BY seq"):
            stream.write(
                encode(
                    {"seq": row["seq"], "kind": row["kind"], **json.loads(row["data"])}
                )
                + "\n"
            )
