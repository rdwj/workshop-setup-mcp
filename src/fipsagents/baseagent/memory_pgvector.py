"""PGVector memory backend with semantic vector search.

Production-grade memory for teams with PGVector in their OpenShift stack.
Uses ``asyncpg`` for async PostgreSQL access and ``httpx`` for embedding
generation via a vLLM-compatible endpoint.

Requires:
  - PostgreSQL with the ``pgvector`` extension
  - An OpenAI-compatible embedding endpoint (e.g. vLLM)
  - ``pip install fipsagents[pgvector]`` (adds ``asyncpg``)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import httpx

from fipsagents.baseagent.memory import MemoryClientBase, NullMemoryClient

logger = logging.getLogger(__name__)

_VALID_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DEFAULT_EMBEDDING_URL = "http://localhost:8000"
_DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
_DEFAULT_EMBEDDING_DIMENSION = 768
_DEFAULT_TABLE_NAME = "agent_memories"
_HTTP_TIMEOUT = 10.0


def _dt_to_iso(value: Any) -> str:
    """Convert asyncpg datetime (or string) to ISO 8601 string."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _embedding_to_str(embedding: list[float]) -> str:
    """Format a float list as the pgvector literal ``[0.1,0.2,...]``."""
    return "[" + ",".join(str(v) for v in embedding) + "]"


async def _get_embedding(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    text: str,
) -> list[float]:
    """Fetch a text embedding from an OpenAI-compatible embeddings endpoint."""
    response = await client.post(
        f"{url}/v1/embeddings",
        json={"input": text, "model": model},
    )
    response.raise_for_status()
    return response.json()["data"][0]["embedding"]


def _build_schema(table: str, dimension: int) -> list[str]:
    """Return idempotent DDL statements for the memory table."""
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"""CREATE TABLE IF NOT EXISTS {table} (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            metadata JSONB,
            embedding vector({dimension}),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        f"""CREATE INDEX IF NOT EXISTS {table}_embedding_idx
            ON {table} USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)""",
    ]


class PGVectorMemoryClient(MemoryClientBase):
    """PostgreSQL + pgvector memory client with semantic search."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        http: httpx.AsyncClient,
        embedding_url: str,
        embedding_model: str,
        embedding_dimension: int,
        table_name: str,
    ) -> None:
        self._pool = pool
        self._http = http
        self._embedding_url = embedding_url
        self._embedding_model = embedding_model
        self._embedding_dimension = embedding_dimension
        self._table = table_name

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        limit = int(kwargs.get("limit", 10))
        try:
            embedding = await _get_embedding(
                self._http, self._embedding_url, self._embedding_model, query
            )
            embedding_str = _embedding_to_str(embedding)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT id, content, metadata, created_at, updated_at"  # noqa: S608
                    f" FROM {self._table}"
                    f" ORDER BY embedding <=> $1::vector LIMIT $2",
                    embedding_str,
                    limit,
                )
            return [_row_to_dict(r) for r in rows]
        except Exception:
            logger.warning(
                "PGVector vector search failed for query %r — trying text fallback",
                query,
                exc_info=True,
            )
        # Text fallback: ILIKE search when embedding is unavailable.
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT id, content, metadata, created_at, updated_at"  # noqa: S608
                    f" FROM {self._table} WHERE content ILIKE $1 LIMIT $2",
                    f"%{query}%",
                    limit,
                )
            return [_row_to_dict(r) for r in rows]
        except Exception:
            logger.warning(
                "PGVector text fallback search also failed for query %r",
                query,
                exc_info=True,
            )
            return []

    async def write(self, content: str, **kwargs: Any) -> dict[str, Any] | None:
        memory_id = str(uuid.uuid4())
        metadata: dict[str, Any] = dict(kwargs.get("metadata") or {})
        if "scope" in kwargs:
            metadata["scope"] = kwargs["scope"]
        if "weight" in kwargs:
            metadata["weight"] = kwargs["weight"]

        embedding_str: str | None = None
        try:
            embedding = await _get_embedding(
                self._http, self._embedding_url, self._embedding_model, content
            )
            embedding_str = _embedding_to_str(embedding)
        except Exception:
            logger.warning(
                "PGVector embedding failed on write — storing without embedding",
                exc_info=True,
            )

        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"INSERT INTO {self._table} (id, content, metadata, embedding)"  # noqa: S608
                    f" VALUES ($1, $2, $3, $4::vector)"
                    f" RETURNING id, content, metadata, created_at",
                    memory_id,
                    content,
                    json.dumps(metadata) if metadata else None,
                    embedding_str,
                )
            return {
                "id": row["id"],
                "content": row["content"],
                "metadata": row["metadata"],
                "created_at": _dt_to_iso(row["created_at"]),
            }
        except Exception:
            logger.warning("PGVector write failed — memory not persisted", exc_info=True)
            return None

    async def update(
        self, memory_id: str, content: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        embedding_str: str | None = None
        try:
            embedding = await _get_embedding(
                self._http, self._embedding_url, self._embedding_model, content
            )
            embedding_str = _embedding_to_str(embedding)
        except Exception:
            logger.warning(
                "PGVector embedding failed on update — keeping existing embedding",
                exc_info=True,
            )

        try:
            # Build SET clause dynamically to avoid four near-identical branches.
            set_parts = ["content=$1", "updated_at=NOW()"]
            params: list[Any] = [content]

            if embedding_str is not None:
                params.append(embedding_str)
                set_parts.insert(1, f"embedding=${len(params)}::vector")

            if "metadata" in kwargs:
                metadata = kwargs["metadata"]
                params.append(json.dumps(metadata) if metadata else None)
                set_parts.append(f"metadata=${len(params)}")

            params.append(memory_id)
            sql = (
                f"UPDATE {self._table} SET {', '.join(set_parts)}"  # noqa: S608
                f" WHERE id=${len(params)} RETURNING id, content, updated_at"
            )

            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, *params)

            if row is None:
                logger.warning("PGVector update: memory %s not found", memory_id)
                return None
            return {
                "id": row["id"],
                "content": row["content"],
                "updated_at": _dt_to_iso(row["updated_at"]),
            }
        except Exception:
            logger.warning(
                "PGVector update failed for memory %s — not persisted",
                memory_id,
                exc_info=True,
            )
            return None

    async def report_contradiction(self, memory_id: str, description: str) -> None:
        logger.warning(
            "Contradiction reported for memory %s: %s "
            "(PGVector backend does not track contradictions server-side)",
            memory_id,
            description,
        )


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "content": row["content"],
        "metadata": row["metadata"],  # asyncpg decodes JSONB → dict automatically
        "created_at": _dt_to_iso(row["created_at"]),
        "updated_at": _dt_to_iso(row["updated_at"]),
    }


async def create_pgvector_client(config_path: Path) -> MemoryClientBase:
    """Create a PGVector-backed memory client from a .memory-pgvector.yaml config.

    Falls back to ``NullMemoryClient`` on any error so the agent always gets
    a usable client.
    """
    try:
        import yaml

        raw = config_path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(raw) or {}

        connection_url: str | None = cfg.get("connection_url")
        if not connection_url:
            logger.error(
                "PGVector config at %s is missing required 'connection_url' — "
                "falling back to NullMemoryClient",
                config_path,
            )
            return NullMemoryClient()

        embedding_url: str = cfg.get("embedding_url", _DEFAULT_EMBEDDING_URL)
        embedding_model: str = cfg.get("embedding_model", _DEFAULT_EMBEDDING_MODEL)
        embedding_dimension: int = int(
            cfg.get("embedding_dimension", _DEFAULT_EMBEDDING_DIMENSION)
        )
        table_name: str = cfg.get("table_name", _DEFAULT_TABLE_NAME)

        if not _VALID_TABLE_NAME.match(table_name):
            logger.error(
                "PGVector table_name %r contains invalid characters — "
                "falling back to NullMemoryClient",
                table_name,
            )
            return NullMemoryClient()

        pool: asyncpg.Pool = await asyncpg.create_pool(connection_url)

        # Optionally register the pgvector codec for richer type support.
        try:
            from pgvector.asyncpg import register_vector  # type: ignore[import]

            async with pool.acquire() as conn:
                await register_vector(conn)
        except ImportError:
            pass  # Will use string representation — fully supported by pgvector

        # Apply schema idempotently.
        statements = _build_schema(table_name, embedding_dimension)
        async with pool.acquire() as conn:
            for stmt in statements:
                try:
                    await conn.execute(stmt)
                except Exception:
                    if "ivfflat" in stmt:
                        logger.warning(
                            "Could not create ivfflat index on %s — table may be empty "
                            "or pgvector version does not support it. Search will work "
                            "without the index, just slower.",
                            table_name,
                        )
                    else:
                        raise

        http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        logger.info("PGVector memory backend enabled (table: %s)", table_name)
        return PGVectorMemoryClient(
            pool=pool,
            http=http_client,
            embedding_url=embedding_url,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            table_name=table_name,
        )
    except Exception:
        logger.warning(
            "Failed to initialise PGVector memory backend from %s — "
            "falling back to NullMemoryClient",
            config_path,
            exc_info=True,
        )
        return NullMemoryClient()
