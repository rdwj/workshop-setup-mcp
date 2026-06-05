"""SQLite memory backend with FTS5 full-text search.

Zero-dependency local memory for development and testing.  Uses Python's
stdlib ``sqlite3`` with FTS5 for keyword search.

Limitations:
  - Keyword search only (no semantic/vector similarity)
  - Single-process safe (WAL mode), not designed for concurrent multi-agent writes
  - No version history on updates
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fipsagents.baseagent.memory import MemoryClientBase, NullMemoryClient

logger = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content='memories',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_metadata(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


class SQLiteMemoryClient(MemoryClientBase):
    """SQLite-backed memory client with FTS5 full-text search."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        limit = int(kwargs.get("limit", 10))
        try:
            return await asyncio.to_thread(self._search_sync, query, limit)
        except Exception:
            logger.warning("SQLite search failed for query %r", query, exc_info=True)
            return []

    def _search_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                SELECT m.id, m.content, m.metadata, m.created_at, m.updated_at
                FROM memories m
                JOIN memories_fts ON memories_fts.rowid = m.rowid
                WHERE memories_fts MATCH ?
                ORDER BY memories_fts.rank
                LIMIT ?
                """,
                (query, limit),
            )
        except sqlite3.OperationalError:
            # Malformed FTS query — fall back to per-word LIKE (OR logic) so
            # that special characters in the query don't produce zero results.
            words = [w for w in re.sub(r"[^\w\s]", " ", query).split() if len(w) >= 2]
            if not words:
                rows = cur.fetchall()  # empty
                return []
            placeholders = " OR ".join("content LIKE ?" for _ in words)
            params: list[Any] = [f"%{w}%" for w in words]
            params.append(limit)
            cur.execute(
                f"SELECT id, content, metadata, created_at, updated_at"  # noqa: S608
                f" FROM memories WHERE {placeholders}"
                f" ORDER BY updated_at DESC LIMIT ?",
                params,
            )
        rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "content": row[1],
                "metadata": _parse_metadata(row[2]),
                "created_at": row[3],
                "updated_at": row[4],
            }
            for row in rows
        ]

    async def write(self, content: str, **kwargs: Any) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(self._write_sync, content, kwargs)
        except Exception:
            logger.warning("SQLite write failed — memory not persisted", exc_info=True)
            return None

    def _write_sync(self, content: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        memory_id = str(uuid.uuid4())
        now = _now()
        metadata: dict[str, Any] = dict(kwargs.get("metadata") or {})
        if "scope" in kwargs:
            metadata["scope"] = kwargs["scope"]
        if "weight" in kwargs:
            metadata["weight"] = kwargs["weight"]
        metadata_json = json.dumps(metadata) if metadata else None
        self._conn.execute(
            "INSERT INTO memories (id, content, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (memory_id, content, metadata_json, now, now),
        )
        self._conn.commit()
        return {
            "id": memory_id,
            "content": content,
            "metadata": metadata or None,
            "created_at": now,
        }

    async def update(
        self, memory_id: str, content: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(self._update_sync, memory_id, content, kwargs)
        except Exception:
            logger.warning(
                "SQLite update failed for memory %s — not persisted",
                memory_id,
                exc_info=True,
            )
            return None

    def _update_sync(
        self, memory_id: str, content: str, kwargs: dict[str, Any]
    ) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        cur.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,))
        row = cur.fetchone()
        if row is None:
            logger.warning("SQLite update: memory %s not found", memory_id)
            return None
        now = _now()
        if "metadata" in kwargs:
            new_meta = kwargs["metadata"]
            metadata_json = json.dumps(new_meta) if new_meta else None
            cur.execute(
                "UPDATE memories SET content = ?, metadata = ?, updated_at = ? WHERE id = ?",
                (content, metadata_json, now, memory_id),
            )
        else:
            cur.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                (content, now, memory_id),
            )
        self._conn.commit()
        return {"id": memory_id, "content": content, "updated_at": now}

    async def report_contradiction(self, memory_id: str, description: str) -> None:
        logger.warning(
            "Contradiction reported for memory %s: %s "
            "(SQLite backend does not track contradictions server-side)",
            memory_id,
            description,
        )


async def create_sqlite_client(config_path: Path) -> MemoryClientBase:
    """Create a SQLite-backed memory client from a .memory-sqlite.yaml config.

    Falls back to ``NullMemoryClient`` on any error so the agent always gets
    a usable client.
    """
    try:
        import yaml

        raw = config_path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(raw) or {}
        db_path_raw = cfg.get("db_path", ".agent-memory.db")
        db_path = Path(db_path_raw)
        if not db_path.is_absolute():
            db_path = config_path.parent / db_path

        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.executescript(_SCHEMA)
        conn.commit()

        logger.info("SQLite memory backend enabled (db: %s)", db_path)
        return SQLiteMemoryClient(conn)
    except Exception:
        logger.warning(
            "Failed to initialise SQLite memory backend from %s — "
            "falling back to NullMemoryClient",
            config_path,
            exc_info=True,
        )
        return NullMemoryClient()
