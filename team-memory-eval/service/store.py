"""每次运行使用一个数据库；每次交棒在同一事务内提交。"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    tmp.replace(path)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            self.directory / "state.sqlite", check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, actor TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mail (id INTEGER PRIMARY KEY, sender TEXT, target TEXT, body TEXT, consumed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, name TEXT NOT NULL, data BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS grants (blob TEXT REFERENCES blobs, actor TEXT, PRIMARY KEY(blob,actor));
        CREATE TABLE IF NOT EXISTS submissions (version INTEGER PRIMARY KEY, slot TEXT, actor TEXT, blob TEXT REFERENCES blobs, turn TEXT);
        CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, actor TEXT, category TEXT, tokens INTEGER, usd REAL, cached_tokens INTEGER NOT NULL DEFAULT 0);
        """)
        if "cached_tokens" not in {
            r["name"] for r in self.db.execute("PRAGMA table_info(usage)")
        }:
            self.db.execute(
                "ALTER TABLE usage ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0"
            )

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        self.db.close()
