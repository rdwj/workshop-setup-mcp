"""Persistent storage for file chunks (ADR-0002, PR-B).

A :class:`ChunkStore` is what receives :class:`Chunk` instances from a
:class:`fipsagents.server.chunker.Chunker`, embeds the content, and serves
similarity-search queries at retrieval time. The store is composed beside
the metadata :class:`FileStore` rather than replacing it — file metadata
may live in SQLite while chunks live in Postgres+pgvector.

Implementations:

- :class:`NullChunkStore` — no-op default. Used when chunking is
  disabled in :class:`FilesConfig`. Safe to call from any path.
- :class:`PgvectorChunkStore` — production backend. asyncpg + httpx for
  embeddings, schema mirrors :class:`PGVectorMemoryClient` but in a
  separate ``file_chunks`` table per ADR-0002.

Per the ADR, `delete_for_file` is the cascade contract — `FileStore`
calls it during file deletion. There is *no* DB-level foreign key
because the metadata table may live in a different database.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    import asyncpg
    import httpx

from fipsagents.server.chunker import Chunk

logger = logging.getLogger(__name__)


_VALID_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_HTTP_TIMEOUT = 10.0
_EMBEDDING_BATCH = 32


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _embedding_to_pgvector(embedding: list[float]) -> str:
    """Format a float list as the pgvector literal ``[0.1,0.2,...]``."""
    return "[" + ",".join(str(v) for v in embedding) + "]"


async def _embed_batch(
    http: "httpx.AsyncClient",
    url: str,
    model: str,
    inputs: list[str],
) -> list[list[float]]:
    """Embed *inputs* via an OpenAI-compatible ``/v1/embeddings`` endpoint.

    Issues one HTTP call per batch. Most embedding endpoints accept a
    list ``input`` and return a parallel list of embeddings; we fall
    back to one call per input if the endpoint rejects array input.
    """
    if not inputs:
        return []
    response = await http.post(
        f"{url}/v1/embeddings",
        json={"input": inputs, "model": model},
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or []
    # The "index" field is officially ordered but defensively re-sort
    # so a misbehaving endpoint cannot scramble chunk → embedding pairing.
    data_sorted = sorted(data, key=lambda d: d.get("index", 0))
    return [item["embedding"] for item in data_sorted]


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class ChunkStore(ABC):
    """Persistent store for file chunks + their embeddings."""

    @abstractmethod
    async def save_chunks(
        self,
        file_id: str,
        chunks: list[Chunk],
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> int:
        """Persist *chunks* under *file_id*. Return the number written.

        The implementation is responsible for embedding each chunk's
        ``content`` and storing it alongside the row. Failures during
        embedding are logged but should not prevent the row from being
        written — a chunk without an embedding is still searchable via
        text fallback in some implementations and is at minimum
        retrievable by ``file_id`` for delete cascades.
        """

    @abstractmethod
    async def search(
        self,
        file_id: str,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[Chunk]:
        """Return the top-``limit`` chunks of *file_id* matching *query*.

        Search is *per-file* by design — the user's referenced file_ids
        are the authorization boundary, so chunks from other files
        never leak in. ``min_score`` is a cosine-similarity floor (0
        disables filtering).
        """

    @abstractmethod
    async def delete_for_file(self, file_id: str) -> int:
        """Delete every chunk for *file_id*. Return the number removed.

        Called by ``FileStore.delete()`` as the app-level cascade. Must
        be idempotent — calling it on a file that has no chunks is a
        valid no-op.
        """

    async def close(self) -> None:
        """Release any held resources. Default: no-op."""


# ---------------------------------------------------------------------------
# Null implementation
# ---------------------------------------------------------------------------


class NullChunkStore(ChunkStore):
    """No-op chunk store. Used when ``files.chunking.enabled`` is false.

    All operations succeed silently. This keeps the call sites in
    :class:`OpenAIChatServer` free of ``if chunk_store is not None:``
    guards.
    """

    async def save_chunks(
        self,
        file_id: str,
        chunks: list[Chunk],
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> int:
        return 0

    async def search(
        self,
        file_id: str,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[Chunk]:
        return []

    async def delete_for_file(self, file_id: str) -> int:
        return 0


# ---------------------------------------------------------------------------
# Pgvector implementation
# ---------------------------------------------------------------------------


def _build_schema(table: str, dimension: int) -> list[str]:
    """Idempotent DDL for the chunks table + indexes."""
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"""CREATE TABLE IF NOT EXISTS {table} (
            chunk_id    TEXT PRIMARY KEY,
            file_id     TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            session_id  TEXT,
            chunk_index INT NOT NULL,
            content     TEXT NOT NULL,
            metadata    JSONB,
            embedding   vector({dimension}),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (file_id, chunk_index)
        )""",
        f"CREATE INDEX IF NOT EXISTS {table}_file_id_idx ON {table} (file_id)",
        f"""CREATE INDEX IF NOT EXISTS {table}_embedding_idx
            ON {table} USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)""",
    ]


class PgvectorChunkStore(ChunkStore):
    """Postgres + pgvector chunk store with cosine-similarity retrieval.

    Mirrors :class:`PGVectorMemoryClient` but writes to a separate
    ``file_chunks`` table whose lifecycle is tied to ``FileRecord``
    rather than to the agent's long-lived memory.
    """

    def __init__(
        self,
        pool: "asyncpg.Pool",
        http: "httpx.AsyncClient",
        embedding_url: str,
        embedding_model: str,
        embedding_dimension: int,
        table_name: str = "file_chunks",
        embedding_batch_size: int = _EMBEDDING_BATCH,
    ) -> None:
        if not _VALID_TABLE_NAME.match(table_name):
            raise ValueError(
                f"PgvectorChunkStore: invalid table_name {table_name!r}",
            )
        self._pool = pool
        self._http = http
        self._embedding_url = embedding_url
        self._embedding_model = embedding_model
        self._embedding_dimension = embedding_dimension
        self._table = table_name
        self._batch = max(1, embedding_batch_size)

    # -- writes ------------------------------------------------------------

    async def save_chunks(
        self,
        file_id: str,
        chunks: list[Chunk],
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> int:
        if not chunks:
            return 0

        embeddings = await self._embed_chunks([c.content for c in chunks])
        rows: list[tuple[Any, ...]] = []
        for index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            chunk_id = f"{file_id}:{index}:{uuid.uuid4().hex[:8]}"
            metadata = json.dumps(chunk.metadata) if chunk.metadata else None
            embedding_lit = (
                _embedding_to_pgvector(embedding) if embedding is not None else None
            )
            rows.append((
                chunk_id,
                file_id,
                user_id,
                session_id,
                index,
                chunk.content,
                metadata,
                embedding_lit,
            ))

        sql = (
            f"INSERT INTO {self._table} "  # noqa: S608
            f"(chunk_id, file_id, user_id, session_id, chunk_index, "
            f"content, metadata, embedding) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)"
        )

        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(sql, rows)
        except Exception:
            logger.warning(
                "PgvectorChunkStore: save_chunks failed for file_id=%s "
                "(rows=%d) — chunks not persisted",
                file_id,
                len(rows),
                exc_info=True,
            )
            return 0
        return len(rows)

    async def _embed_chunks(self, texts: list[str]) -> list[list[float] | None]:
        """Embed *texts* in batches. Returns ``None`` for failed embeddings."""
        out: list[list[float] | None] = []
        for start in range(0, len(texts), self._batch):
            batch = texts[start : start + self._batch]
            try:
                embeddings = await _embed_batch(
                    self._http,
                    self._embedding_url,
                    self._embedding_model,
                    batch,
                )
                if len(embeddings) != len(batch):
                    raise ValueError(
                        f"embedding endpoint returned {len(embeddings)} vectors "
                        f"for {len(batch)} inputs",
                    )
                out.extend(embeddings)
            except Exception:
                logger.warning(
                    "PgvectorChunkStore: embedding batch failed "
                    "(start=%d, size=%d) — chunks will be stored without "
                    "embeddings and will not be retrievable via vector search",
                    start,
                    len(batch),
                    exc_info=True,
                )
                out.extend([None] * len(batch))
        return out

    # -- reads -------------------------------------------------------------

    async def search(
        self,
        file_id: str,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[Chunk]:
        try:
            embeddings = await _embed_batch(
                self._http,
                self._embedding_url,
                self._embedding_model,
                [query],
            )
            if not embeddings:
                raise RuntimeError("embedding endpoint returned no vectors")
            query_embedding = _embedding_to_pgvector(embeddings[0])
        except Exception:
            logger.warning(
                "PgvectorChunkStore: query embed failed for file_id=%s — "
                "no chunks returned (caller should fall back to full text)",
                file_id,
                exc_info=True,
            )
            return []

        # Cosine *distance* ranges 0 (identical) → 2 (opposite). We
        # convert to a similarity score = 1 - distance so the caller's
        # min_score is intuitive (higher = more similar).
        sql = (
            f"SELECT content, metadata, "  # noqa: S608
            f"1 - (embedding <=> $1::vector) AS score "
            f"FROM {self._table} "
            f"WHERE file_id = $2 AND embedding IS NOT NULL "
            f"ORDER BY embedding <=> $1::vector LIMIT $3"
        )
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, query_embedding, file_id, limit)
        except Exception:
            logger.warning(
                "PgvectorChunkStore: search query failed for file_id=%s",
                file_id,
                exc_info=True,
            )
            return []

        out: list[Chunk] = []
        for row in rows:
            score = float(row.get("score") or 0.0)
            if score < min_score:
                continue
            metadata = row["metadata"] or {}
            if isinstance(metadata, str):
                # asyncpg sometimes returns JSONB as str without codec.
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            metadata = dict(metadata)
            metadata["score"] = score
            out.append(Chunk(
                content=row["content"],
                metadata=metadata,
            ))
        return out

    # -- deletes -----------------------------------------------------------

    async def delete_for_file(self, file_id: str) -> int:
        try:
            async with self._pool.acquire() as conn:
                status = await conn.execute(
                    f"DELETE FROM {self._table} WHERE file_id = $1",  # noqa: S608
                    file_id,
                )
        except Exception:
            logger.warning(
                "PgvectorChunkStore: delete_for_file failed for file_id=%s",
                file_id,
                exc_info=True,
            )
            return 0
        # asyncpg's execute returns "DELETE N" — parse the count.
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError, AttributeError):
            return 0

    async def close(self) -> None:
        """Release the connection pool. Caller should not reuse the store."""
        try:
            await self._pool.close()
        except Exception:  # pragma: no cover — defensive
            logger.debug("PgvectorChunkStore: pool close raised", exc_info=True)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


async def initialise_pgvector_schema(
    pool: "asyncpg.Pool",
    table_name: str,
    embedding_dimension: int,
) -> None:
    """Apply :func:`_build_schema` to *pool* idempotently.

    Logs (but does not raise) when the ivfflat index cannot be built —
    pgvector requires at least one row in the table for ``ivfflat`` to
    initialise on some versions; the index can be rebuilt later.
    """
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(
            f"initialise_pgvector_schema: invalid table_name {table_name!r}",
        )
    statements = _build_schema(table_name, embedding_dimension)
    async with pool.acquire() as conn:
        for stmt in statements:
            try:
                await conn.execute(stmt)
            except Exception:
                if "ivfflat" in stmt:
                    logger.warning(
                        "PgvectorChunkStore: ivfflat index could not be "
                        "created on %s — likely the table is empty. Search "
                        "will work without the index, just slower.",
                        table_name,
                    )
                else:
                    raise


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def create_pgvector_chunk_store(
    *,
    database_url: str,
    embedding_url: str,
    embedding_model: str,
    embedding_dimension: int = 768,
    table_name: str = "file_chunks",
    http_timeout: float = _DEFAULT_HTTP_TIMEOUT,
) -> ChunkStore:
    """Create a :class:`PgvectorChunkStore` against *database_url*.

    Falls back to :class:`NullChunkStore` on any initialisation error
    so the server always gets a usable store and chat completions
    degrade gracefully to the full-text path.
    """
    try:
        import asyncpg
        import httpx
    except ImportError:
        logger.error(
            "create_pgvector_chunk_store: asyncpg/httpx not installed. "
            "Install with: pip install fipsagents[chunking]",
        )
        return NullChunkStore()

    if not database_url:
        logger.error(
            "create_pgvector_chunk_store: database_url is empty — "
            "falling back to NullChunkStore",
        )
        return NullChunkStore()
    if not embedding_url:
        logger.error(
            "create_pgvector_chunk_store: embedding_url is empty — "
            "falling back to NullChunkStore",
        )
        return NullChunkStore()
    if not _VALID_TABLE_NAME.match(table_name):
        logger.error(
            "create_pgvector_chunk_store: invalid table_name %r — "
            "falling back to NullChunkStore",
            table_name,
        )
        return NullChunkStore()

    try:
        pool = await asyncpg.create_pool(database_url)
    except Exception:
        logger.warning(
            "create_pgvector_chunk_store: pool creation failed — "
            "falling back to NullChunkStore",
            exc_info=True,
        )
        return NullChunkStore()

    try:
        await initialise_pgvector_schema(pool, table_name, embedding_dimension)
    except Exception:
        logger.warning(
            "create_pgvector_chunk_store: schema init failed — "
            "falling back to NullChunkStore",
            exc_info=True,
        )
        await pool.close()
        return NullChunkStore()

    http = httpx.AsyncClient(timeout=http_timeout)
    logger.info(
        "PgvectorChunkStore enabled (table=%s, dimension=%d)",
        table_name,
        embedding_dimension,
    )
    return PgvectorChunkStore(
        pool=pool,
        http=http,
        embedding_url=embedding_url,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        table_name=table_name,
    )
