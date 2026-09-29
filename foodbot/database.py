"""Single-owner state, durable inbox/outbox, and purchase ledger.

Each accepted update changes state and creates its reply in one SQLite transaction.
Telegram delivery is at-least-once; a crash after delivery can duplicate a message,
but never applies a grocery mutation twice.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


class Database:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY, payload TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, payload TEXT NOT NULL, kind TEXT NOT NULL,
                    generation INTEGER, sent INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY, bought_at TEXT NOT NULL, items TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def enqueue(self, update):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO inbox(id,payload) VALUES (?,?)",
                       (update["update_id"], json.dumps(update)))

    @staticmethod
    def read(db):
        row = db.execute("SELECT data FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def save(db, state):
        db.execute("INSERT INTO state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                   (json.dumps(state),))

    @staticmethod
    def queue(db, text, buttons=None, kind="reply", generation=None):
        payload = {"text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        db.execute("INSERT INTO outbox(payload,kind,generation) VALUES (?,?,?)",
                   (json.dumps(payload), kind, generation))

    def snapshot(self):
        with self.connect() as db:
            return self.read(db)
