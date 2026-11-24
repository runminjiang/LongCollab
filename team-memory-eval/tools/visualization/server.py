"""从本机运行目录读取已提交事件和原生会话，提供只读观察界面。"""

import argparse
import json
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .history import native_history

ASSETS = Path(__file__).resolve().parent
DEFAULT_RUNS = ASSETS.parents[1] / "experiments" / "runs"


def read_json(path):
    if path.is_file() and not path.is_symlink():
        return json.loads(path.read_text())
    return {}


def is_run(path):
    return (
        path.is_dir()
        and not path.is_symlink()
        and any(
            (path / name).is_file() and not (path / name).is_symlink()
            for name in ("run.json", "events.jsonl", "state.sqlite")
        )
    )


def run_list(root):
    if is_run(root):
        return [root]
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if is_run(p)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def database(path):
    path = path / "state.sqlite"
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError("此轨迹没有数据库")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.75)


def snapshot(path, after):
    state = {}
    has_db = (path / "state.sqlite").is_file() and not (
        path / "state.sqlite"
    ).is_symlink()
    if has_db:
        with closing(database(path)) as db:
            db.execute("BEGIN")
            # run.json 会先于运行库的建表操作出现。
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            events = (
                [
                    {**json.loads(data), "seq": seq, "kind": kind}
                    for seq, kind, data in db.execute(
                        "SELECT seq,kind,data FROM events WHERE seq>? ORDER BY seq",
                        (after,),
                    )
                ]
                if "events" in tables
                else []
            )
            if "state" in tables:
                row = db.execute("SELECT data FROM state WHERE key='team'").fetchone()
                state = json.loads(row[0]) if row else {}
    else:
        events = []
        log = path / "events.jsonl"
        if log.is_file() and not log.is_symlink():
            with log.open() as stream:
                for line in stream:
                    if line.strip():
                        event = json.loads(line)
                        if event["seq"] > after:
                            events.append(event)

    manifest = read_json(path / "run.json")
    result = read_json(path / "result.json")
    return {
        "config": manifest.get("config", {}),
        "started": manifest.get("started"),
        "events": events,
        "members": state.get("members") or result.get("members", {}),
        "plan": read_json(path / "plan.json"),
        "result": result,
        "scores": read_json(path / "scores.json"),
        "has_db": has_db,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, data, mime="application/json", *, status=200, disposition=None):
        if mime == "application/json":
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'",
        )
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self.route()
        except (FileNotFoundError, KeyError):
            self.reply({"error": "找不到这份轨迹或附件"}, status=404)
        except (ValueError, UnicodeError):
            self.reply({"error": "轨迹格式或请求参数无效"}, status=400)
        except (sqlite3.Error, OSError) as exc:
            if not isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                self.reply({"error": "暂时无法读取轨迹，请重试"}, status=503)

    def route(self):
        if self.headers.get("Host", "").split(":")[0] not in {"127.0.0.1", "localhost"}:
            self.reply({"error": "仅接受本机访问"}, status=403)
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        name = url.path.lstrip("/") or "index.html"
        assets = {
            "index.html": "text/html; charset=utf-8",
            "app.js": "text/javascript",
            "style.css": "text/css",
            "example.jsonl": "text/plain",
        }
        if name in assets:
            self.reply((ASSETS / name).read_bytes(), assets[name])
            return
        root = self.server.runs_root
        if name == "api/runs":
            self.reply([p.name for p in run_list(root)])
            return
        run = query.get("run", [""])[0]
        path = root if root.name == run and is_run(root) else root / run
        if not run or run in {".", ".."} or Path(run).name != run or not is_run(path):
            raise FileNotFoundError
        if name == "api/trace":
            self.reply(snapshot(path, max(0, int(query.get("after", [0])[0]))))
        elif name == "api/history":
            self.reply(
                native_history(path, query["member"][0], query.get("version", [""])[0])
            )
        elif name == "api/file":
            with closing(database(path)) as db:
                row = db.execute(
                    "SELECT name,data FROM blobs WHERE id=?", (query["id"][0],)
                ).fetchone()
            if not row:
                raise FileNotFoundError
            self.reply(
                row[1],
                "application/octet-stream",
                disposition="attachment; filename*=UTF-8''" + quote(row[0], safe=""),
            )
        else:
            raise FileNotFoundError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-dir", type=Path, default=DEFAULT_RUNS, help="运行集合或单次运行的目录"
    )
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", options.port), Handler) as server:
        server.runs_root = options.runs_dir.resolve()
        print(
            f"观察界面 http://127.0.0.1:{server.server_port}/  ·  {server.runs_root}",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
