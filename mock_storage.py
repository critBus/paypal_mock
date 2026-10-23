"""Small durable stores for provider resources; imports do not touch local databases."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path


class ResourceStore(MutableMapping):
    """Persist explicit assignments; callers save a resource again after changing nested data."""

    def __init__(self, provider: str, collection: str) -> None:
        self.provider = provider
        self.collection = collection
        base = Path(__file__).resolve().parent / "data" / "scenarios.sqlite3"
        self.path = Path(os.getenv("MOCK_SCENARIOS_DB_PATH", str(base)))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Initialize lazily and commit before an API response or webhook can observe the result."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS mock_resources "
                    "(provider TEXT, collection TEXT, id TEXT, data TEXT, PRIMARY KEY(provider, collection, id))"
                )
                yield connection
        finally:
            connection.close()

    def __getitem__(self, key: str) -> dict:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT data FROM mock_resources WHERE provider=? AND collection=? AND id=?",
                (self.provider, self.collection, key),
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __setitem__(self, key: str, value: dict) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO mock_resources VALUES(?,?,?,?) ON CONFLICT(provider,collection,id) "
                "DO UPDATE SET data=excluded.data",
                (self.provider, self.collection, key, json.dumps(value)),
            )

    def __delitem__(self, key: str) -> None:
        with self.connection() as connection:
            if not connection.execute(
                "DELETE FROM mock_resources WHERE provider=? AND collection=? AND id=?",
                (self.provider, self.collection, key),
            ).rowcount:
                raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM mock_resources WHERE provider=? AND collection=? ORDER BY rowid",
                (self.provider, self.collection),
            ).fetchall()
        return iter(row[0] for row in rows)

    def __len__(self) -> int:
        with self.connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM mock_resources WHERE provider=? AND collection=?",
                (self.provider, self.collection),
            ).fetchone()[0]

    def clear(self) -> None:
        """Reset this collection only, leaving other providers and scenarios intact."""
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM mock_resources WHERE provider=? AND collection=?",
                (self.provider, self.collection),
            )
