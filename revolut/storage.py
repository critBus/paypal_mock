"""Persistencia SQLite del mock de Revolut, sin dependencias externas."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class RevolutStorage:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self):
        # Apertura diferida: importar la app no crea ni modifica datos.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS revolut_orders "
                    "(id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, data TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS revolut_webhooks "
                    "(id TEXT PRIMARY KEY, data TEXT NOT NULL)"
                )
                yield connection
        finally:
            connection.close()

    def save_order(self, order: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO revolut_orders (id, token, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET token=excluded.token, data=excluded.data",
                (order["id"], order["token"], json.dumps(order)),
            )

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT data FROM revolut_orders WHERE id = ?", (order_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get_order_by_token(self, token: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT data FROM revolut_orders WHERE token = ?", (token,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_webhook(self, webhook: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO revolut_webhooks (id, data) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (webhook["id"], json.dumps(webhook)),
            )

    def get_webhook(self, webhook_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT data FROM revolut_webhooks WHERE id = ?", (webhook_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_webhooks(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT data FROM revolut_webhooks ORDER BY rowid"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete_webhook(self, webhook_id: str) -> bool:
        with self._connection() as connection:
            return (
                connection.execute(
                    "DELETE FROM revolut_webhooks WHERE id = ?", (webhook_id,)
                ).rowcount
                > 0
            )
