"""Where paused runs and the approval queue are kept.

With a database URL: Postgres. LangGraph's checkpoints go in the `checkpoints`
schema and the queue records in `app`, matching infra/postgres/init. Without
one: in memory, which is fine for a demo but forgets everything on restart.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore


@dataclass
class Storage:
    checkpointer: BaseCheckpointSaver
    store: BaseStore
    kind: str  # "postgres" or "memory"


def database_url() -> str | None:
    """COPILOT_DATABASE_URL, or the docker-compose Postgres if POSTGRES_PASSWORD is set."""
    if url := os.environ.get("COPILOT_DATABASE_URL"):
        return url
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        return None
    user = os.environ.get("POSTGRES_USER", "copilot")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql://{quote(user)}:{quote(password, safe='')}@{host}:{port}/copilot"


def with_schema(url: str, schema: str) -> str:
    """The same database, with tables created in and read from `schema`."""
    parts = urlsplit(url)
    option = f"options=-csearch_path%3D{schema}"
    query = f"{parts.query}&{option}" if parts.query else option
    return urlunsplit(parts._replace(query=query))


async def _ensure_schemas(url: str, *schemas: str) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        for schema in schemas:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


@asynccontextmanager
async def open_storage(url: str | None) -> AsyncIterator[Storage]:
    if not url:
        yield Storage(InMemorySaver(), InMemoryStore(), "memory")
        return

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    await _ensure_schemas(url, "checkpoints", "app")
    async with (
        AsyncPostgresSaver.from_conn_string(with_schema(url, "checkpoints")) as checkpointer,
        AsyncPostgresStore.from_conn_string(with_schema(url, "app")) as store,
    ):
        await checkpointer.setup()  # creates or migrates its tables; safe to run every start
        await store.setup()
        yield Storage(checkpointer, store, "postgres")
