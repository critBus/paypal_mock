"""Estado SQLite persistente, con transacciones para operaciones de un solo uso."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class PlaidStorage:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS plaid_records (kind TEXT, id TEXT, owner TEXT, data TEXT, PRIMARY KEY(kind,id))"
            )
            db.execute("BEGIN IMMEDIATE")
            yield Records(db)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class Records:
    def __init__(self, db):
        self.db = db

    def get(self, kind, key, owner=None):
        row = self.db.execute(
            "SELECT owner,data FROM plaid_records WHERE kind=? AND id=?", (kind, key)
        ).fetchone()
        if not row or (owner is not None and row[0] != owner):
            return None
        return json.loads(row[1])

    def put(self, kind, key, owner, data):
        self.db.execute(
            "INSERT INTO plaid_records VALUES (?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET owner=excluded.owner,data=excluded.data",
            (kind, key, owner, json.dumps(data)),
        )

    def list(self, kind, owner):
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT data FROM plaid_records WHERE kind=? AND owner=? ORDER BY rowid",
                (kind, owner),
            )
        ]
