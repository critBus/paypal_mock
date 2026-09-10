"""Persistent, atomic transitions for mock BMSPay hosted links."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class PaymentLinkStorage:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS bms_payment_links "
                    "(id TEXT PRIMARY KEY, merchant TEXT NOT NULL, invoice TEXT NOT NULL, data TEXT NOT NULL)"
                )
                yield connection
        finally:
            connection.close()

    def create(self, link):
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO bms_payment_links VALUES (?, ?, ?, ?)",
                (
                    link["Id"],
                    str(link["MerchantId"]),
                    link["InvoiceNumber"],
                    json.dumps(link),
                ),
            )

    def get(self, identifier, merchant=None):
        with self.connection() as connection:
            query = "SELECT data FROM bms_payment_links WHERE (id = ? OR invoice = ?)"
            args = [identifier, identifier]
            if merchant is not None:
                query += " AND merchant = ?"
                args.append(str(merchant))
            row = connection.execute(
                query + " ORDER BY rowid DESC LIMIT 1", args
            ).fetchone()
            return json.loads(row[0]) if row else None

    def list(self, merchant):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT data FROM bms_payment_links WHERE merchant = ? ORDER BY rowid",
                (str(merchant),),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def update(self, identifier, mutate):
        """Read and mutate while holding a write lock, including across workers."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM bms_payment_links WHERE id = ?", (identifier,)
            ).fetchone()
            if row is None:
                return None
            link = json.loads(row[0])
            result = mutate(link)
            connection.execute(
                "UPDATE bms_payment_links SET data = ? WHERE id = ?",
                (json.dumps(link), identifier),
            )
            return result
