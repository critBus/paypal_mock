"""Lazy, transactionally updated SQLite state owned by the Redsys mock."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class RedsysStorage:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS redsys_state (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def get(connection, identifier):
        row = connection.execute(
            "SELECT data FROM redsys_state WHERE id=?", (identifier,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def save(connection, identifier, value):
        connection.execute(
            "INSERT INTO redsys_state VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (identifier, json.dumps(value)),
        )

    @staticmethod
    def links(connection):
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT data FROM redsys_state WHERE id LIKE 'link:%' ORDER BY rowid DESC"
            )
        ]
