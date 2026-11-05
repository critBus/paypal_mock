"""Noon-only durable state; imports never create or reset databases."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class NoonStorage:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS noon_orders (id TEXT PRIMARY KEY, reference TEXT UNIQUE, token TEXT UNIQUE, data TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS noon_settings (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS noon_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)"
                )
                yield db
        finally:
            db.close()

    @staticmethod
    def dump(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def create(self, value):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT data FROM noon_orders WHERE reference=?", (value["reference"],)
            ).fetchone()
            if existing:
                return json.loads(existing[0]), False
            db.execute(
                "INSERT INTO noon_orders VALUES (?,?,?,?)",
                (
                    str(value["id"]),
                    value["reference"],
                    value["_token"],
                    self.dump(value),
                ),
            )
        return value, True

    def get(self, identifier, *, by="id"):
        if by not in {"id", "reference", "token"}:
            raise ValueError("Unsupported lookup")
        with self.connection() as db:
            row = db.execute(
                f"SELECT data FROM noon_orders WHERE {by}=?", (str(identifier),)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def mutate(self, identifier, callback):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM noon_orders WHERE id=?", (str(identifier),)
            ).fetchone()
            if row is None:
                return None
            value = json.loads(row[0])
            result = callback(value)
            db.execute(
                "UPDATE noon_orders SET data=? WHERE id=?",
                (self.dump(value), str(identifier)),
            )
            return value, result

    def orders(self):
        with self.connection() as db:
            rows = db.execute(
                "SELECT data FROM noon_orders ORDER BY rowid DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def defaults(self, value=None):
        with self.connection() as db:
            if value is not None:
                db.execute(
                    "INSERT INTO noon_settings VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    ("defaults", self.dump(value)),
                )
            row = db.execute(
                "SELECT data FROM noon_settings WHERE id=?", ("defaults",)
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def record_delivery(self, value):
        with self.connection() as db:
            db.execute(
                "INSERT INTO noon_deliveries (data) VALUES (?)", (self.dump(value),)
            )

    def deliveries(self):
        with self.connection() as db:
            rows = db.execute(
                "SELECT data FROM noon_deliveries ORDER BY id DESC LIMIT 200"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
