"""Thread persistence.

Deliberately NOT Postgres. A Flexible Server B1ms would be ~$13/mo -- more than
every other line item in this stack combined. Azure Table Storage costs pennies;
the in-memory backend is the default for local dev.

Swap by setting TABLES_CONNECTION_STRING.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Store(Protocol):
    async def save(self, thread_id: str, data: dict[str, Any]) -> None: ...
    async def load(self, thread_id: str) -> dict[str, Any] | None: ...
    async def list_threads(self, limit: int = 50) -> list[dict[str, Any]]: ...
    async def delete(self, thread_id: str) -> None: ...


class MemoryStore:
    """Process-local. Fine for dev; resets on scale-to-zero."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def save(self, thread_id: str, data: dict[str, Any]) -> None:
        self._data[thread_id] = {**data, "updated_at": time.time()}

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        return self._data.get(thread_id)

    async def list_threads(self, limit: int = 50) -> list[dict[str, Any]]:
        items = sorted(
            self._data.values(), key=lambda d: d.get("updated_at", 0), reverse=True
        )
        return [
            {"id": i["id"], "title": i.get("title", ""), "updated_at": i["updated_at"]}
            for i in items[:limit]
        ]

    async def delete(self, thread_id: str) -> None:
        self._data.pop(thread_id, None)


class TableStore:
    """Azure Table Storage. Cheap, durable, survives scale-to-zero."""

    def __init__(self, conn: str, table: str = "threads") -> None:
        from azure.data.tables.aio import TableServiceClient

        self._svc = TableServiceClient.from_connection_string(conn)
        self._table_name = table
        self._client = None

    async def _table(self):
        if self._client is None:
            self._client = self._svc.get_table_client(self._table_name)
            try:
                await self._client.create_table()
            except Exception:  # noqa: BLE001 - already exists
                pass
        return self._client

    async def save(self, thread_id: str, data: dict[str, Any]) -> None:
        table = await self._table()
        await table.upsert_entity(
            {
                "PartitionKey": "thread",
                "RowKey": thread_id,
                "title": data.get("title", ""),
                "payload": json.dumps(data),
                "updated_at": time.time(),
            }
        )

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        table = await self._table()
        try:
            entity = await table.get_entity("thread", thread_id)
        except Exception:  # noqa: BLE001
            return None
        return json.loads(entity["payload"])

    async def list_threads(self, limit: int = 50) -> list[dict[str, Any]]:
        table = await self._table()
        out = []
        async for e in table.list_entities():
            out.append(
                {"id": e["RowKey"], "title": e.get("title", ""),
                 "updated_at": e.get("updated_at", 0)}
            )
        out.sort(key=lambda d: d["updated_at"], reverse=True)
        return out[:limit]

    async def delete(self, thread_id: str) -> None:
        table = await self._table()
        try:
            await table.delete_entity("thread", thread_id)
        except Exception:  # noqa: BLE001
            pass


def get_store() -> Store:
    conn = os.getenv("TABLES_CONNECTION_STRING")
    if conn:
        try:
            return TableStore(conn)
        except Exception as exc:  # noqa: BLE001
            log.warning("Table Storage unavailable (%s); using memory store", exc)
    return MemoryStore()


def new_thread_id() -> str:
    return uuid.uuid4().hex[:16]


store: Store = get_store()
