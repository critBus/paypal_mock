"""Persistencia SQLite para el mock local de Stripe."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


class StripeMockStorage:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS stripe_payment_links "
                    "(id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, data TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS stripe_checkout_sessions "
                    "(id TEXT PRIMARY KEY, payment_link_id TEXT NOT NULL, data TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS stripe_payment_intents "
                    "(id TEXT PRIMARY KEY, payment_link_id TEXT NOT NULL, data TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS stripe_webhook_endpoints "
                    "(id TEXT PRIMARY KEY, data TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS stripe_webhook_deliveries "
                    "(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, data TEXT NOT NULL)"
                )
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _loads(row):
        return json.loads(row[0]) if row else None

    @staticmethod
    def _dump(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def save_payment_link(self, value: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO stripe_payment_links (id, token, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET token=excluded.token, data=excluded.data",
                (value["id"], value["_token"], self._dump(value)),
            )

    def get_payment_link(self, identifier: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT data FROM stripe_payment_links WHERE id = ?", (identifier,)
            ).fetchone()
        return self._loads(row)

    def get_payment_link_by_token(self, token: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT data FROM stripe_payment_links WHERE token = ?", (token,)
            ).fetchone()
        return self._loads(row)

    def list_payment_links(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT data FROM stripe_payment_links ORDER BY rowid DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_payment_link(
        self,
        identifier: str,
        mutate: Callable[[dict[str, Any]], Any],
    ) -> Any:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM stripe_payment_links WHERE id = ?", (identifier,)
            ).fetchone()
            if row is None:
                return None
            value = json.loads(row[0])
            result = mutate(value)
            connection.execute(
                "UPDATE stripe_payment_links SET token = ?, data = ? WHERE id = ?",
                (value["_token"], self._dump(value), identifier),
            )
            return result

    def save_checkout_session(self, value: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO stripe_checkout_sessions (id, payment_link_id, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payment_link_id=excluded.payment_link_id, data=excluded.data",
                (value["id"], value["payment_link"], self._dump(value)),
            )

    def get_checkout_session(self, identifier: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT data FROM stripe_checkout_sessions WHERE id = ?", (identifier,)
            ).fetchone()
        return self._loads(row)

    def list_checkout_sessions(
        self,
        *,
        payment_link_id: str | None = None,
        payment_intent_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT data FROM stripe_checkout_sessions ORDER BY rowid DESC"
            ).fetchall()
        values = [json.loads(row[0]) for row in rows]
        if payment_link_id:
            values = [item for item in values if item.get("payment_link") == payment_link_id]
        if payment_intent_id:
            values = [item for item in values if item.get("payment_intent") == payment_intent_id]
        return values[:limit]

    def save_payment_intent(self, value: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO stripe_payment_intents (id, payment_link_id, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payment_link_id=excluded.payment_link_id, data=excluded.data",
                (value["id"], value["_payment_link_id"], self._dump(value)),
            )

    def get_payment_intent(self, identifier: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT data FROM stripe_payment_intents WHERE id = ?", (identifier,)
            ).fetchone()
        return self._loads(row)

    def list_payment_intents(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT data FROM stripe_payment_intents ORDER BY rowid DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_webhook_endpoint(self, value: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO stripe_webhook_endpoints (id, data) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (value["id"], self._dump(value)),
            )

    def get_webhook_endpoint(self, identifier: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT data FROM stripe_webhook_endpoints WHERE id = ?", (identifier,)
            ).fetchone()
        return self._loads(row)

    def list_webhook_endpoints(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT data FROM stripe_webhook_endpoints ORDER BY rowid"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete_webhook_endpoint(self, identifier: str) -> bool:
        with self.connection() as connection:
            return (
                connection.execute(
                    "DELETE FROM stripe_webhook_endpoints WHERE id = ?", (identifier,)
                ).rowcount
                > 0
            )

    def add_webhook_delivery(self, event_id: str, value: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO stripe_webhook_deliveries (event_id, data) VALUES (?, ?)",
                (event_id, self._dump(value)),
            )

    def list_webhook_deliveries(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT data FROM stripe_webhook_deliveries ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def reset(self) -> None:
        with self.connection() as connection:
            for table in (
                "stripe_webhook_deliveries",
                "stripe_checkout_sessions",
                "stripe_payment_intents",
                "stripe_payment_links",
                "stripe_webhook_endpoints",
            ):
                connection.execute(f"DELETE FROM {table}")
