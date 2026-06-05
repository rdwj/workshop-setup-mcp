"""Session persistence backends.

Stores and retrieves conversation message histories keyed by session ID.
The server loads messages before processing a request and saves after
the response completes. When no session store is configured, the
``NullSessionStore`` provides backward-compatible ephemeral behavior.
"""

from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _generate_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:16]}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore(ABC):
    """Pluggable session persistence backend."""

    @abstractmethod
    async def create(
        self,
        session_id: str | None = None,
        *,
        parent_session_id: str | None = None,
        forked_at_message_id: str | None = None,
        permission_scope_active: str | None = None,
    ) -> str:
        """Create a session. Generate ID if not provided."""

    @abstractmethod
    async def load(self, session_id: str) -> list[dict] | None:
        """Load messages for a session. None if not found."""

    @abstractmethod
    async def save(self, session_id: str, messages: list[dict]) -> None:
        """Persist the full message history for a session."""

    @abstractmethod
    async def update(
        self,
        session_id: str,
        *,
        cost_data: dict | None = None,
    ) -> bool:
        """Partial update of a session.

        Currently supports merging ``cost_data`` (shallow merge per top-level key,
        write-wins). Returns True if the session existed, False otherwise.
        Designed to be additive -- future fields can be added as keyword-only args.
        """

    @abstractmethod
    async def get_cost_data(self, session_id: str) -> dict:
        """Return the current accumulated ``cost_data`` for a session.

        Symmetric companion to :meth:`update` so callers (notably the
        server's per-turn cost accumulator) can read the existing totals
        before computing the next write.

        Returns an empty dict if the session is missing or has no
        cost_data yet. Backends without a read endpoint (notably the
        HTTP-backed store) raise :class:`NotImplementedError`.
        """

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """Remove a session. Return True if it existed."""

    @abstractmethod
    async def exists(self, session_id: str) -> bool:
        """Check if a session exists."""

    @abstractmethod
    async def delete_before(self, cutoff: datetime) -> int:
        """Delete sessions not updated since *cutoff*. Return count deleted."""

    async def update_state(self, session_id: str, **fields: Any) -> bool:
        """Update session state fields. Default no-op for backward compat.

        Supported fields: pending_question, open_tool_calls,
        pending_subagent_calls, permission_scope_active, compaction_state.
        """
        return False

    async def get_state(self, session_id: str) -> dict[str, Any]:
        """Return session state fields. Default returns empty dict."""
        return {}

    async def fork(
        self,
        session_id: str,
        from_message_index: int | None = None,
    ) -> str:
        """Branch a session at the given message index.

        Returns a new session_id with messages[0:from_message_index] copied.
        None means fork at the current end (full copy).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support fork"
        )

    async def revert(
        self,
        session_id: str,
        to_message_index: int,
    ) -> None:
        """Truncate the session's messages to the given index."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support revert"
        )

    async def close(self) -> None:
        """Release resources. Default is a no-op."""


class NullSessionStore(SessionStore):
    """No persistence -- every request is ephemeral."""

    async def create(
        self,
        session_id: str | None = None,
        *,
        parent_session_id: str | None = None,
        forked_at_message_id: str | None = None,
        permission_scope_active: str | None = None,
    ) -> str:
        sid = session_id or _generate_session_id()
        logger.debug("NullSessionStore: created (ephemeral) %s", sid)
        return sid

    async def load(self, session_id: str) -> list[dict] | None:
        return None

    async def save(self, session_id: str, messages: list[dict]) -> None:
        pass

    async def update(
        self,
        session_id: str,
        *,
        cost_data: dict | None = None,
    ) -> bool:
        return False

    async def get_cost_data(self, session_id: str) -> dict:
        return {}

    async def delete(self, session_id: str) -> bool:
        return False

    async def exists(self, session_id: str) -> bool:
        return False

    async def delete_before(self, cutoff: datetime) -> int:
        return 0


class SqliteSessionStore(SessionStore):
    """Single-file session persistence via aiosqlite."""

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    messages    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    cost_data   TEXT NOT NULL DEFAULT '{}'
)"""

    def __init__(self, db_path: str = "./agent.db", *, connection: Any = None) -> None:
        self._db_path = db_path
        self._db: Any = connection  # Pre-set if managed
        self._managed = connection is not None
        self._initialized = False

    async def _get_db(self) -> Any:
        if self._db is None:
            import aiosqlite

            self._db = await aiosqlite.connect(self._db_path)
        if not self._initialized:
            await self._ensure_table()
        return self._db

    async def _ensure_table(self) -> None:
        db = self._db
        await db.execute(self._CREATE_TABLE)
        # Migrate older databases that predate cost_data.
        cursor = await db.execute("PRAGMA table_info(sessions)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "cost_data" not in cols:
            await db.execute(
                "ALTER TABLE sessions ADD COLUMN cost_data TEXT NOT NULL DEFAULT '{}'"
            )
            logger.debug("SqliteSessionStore: migrated schema (added cost_data)")
        # Migrate to add new state columns.
        new_cols = {
            "parent_session_id": "TEXT DEFAULT NULL",
            "forked_at_message_id": "TEXT DEFAULT NULL",
            "pending_question": "TEXT DEFAULT NULL",
            "open_tool_calls": "TEXT NOT NULL DEFAULT '[]'",
            "pending_subagent_calls": "TEXT NOT NULL DEFAULT '[]'",
            "permission_scope_active": "TEXT DEFAULT NULL",
            "compaction_state": "TEXT NOT NULL DEFAULT '{}'",
            "checkpoint_state": "TEXT DEFAULT NULL",
        }
        cursor = await db.execute("PRAGMA table_info(sessions)")
        existing = {row[1] for row in await cursor.fetchall()}
        for col_name, col_def in new_cols.items():
            if col_name not in existing:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_def}")
                logger.debug("SqliteSessionStore: migrated schema (added %s)", col_name)
        await db.commit()
        self._initialized = True

    async def create(
        self,
        session_id: str | None = None,
        *,
        parent_session_id: str | None = None,
        forked_at_message_id: str | None = None,
        permission_scope_active: str | None = None,
    ) -> str:
        sid = session_id or _generate_session_id()
        now = _utc_now_iso()
        db = await self._get_db()
        await db.execute(
            "INSERT INTO sessions "
            "(session_id, messages, created_at, updated_at, parent_session_id, "
            "forked_at_message_id, permission_scope_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "[]", now, now, parent_session_id, forked_at_message_id,
             permission_scope_active),
        )
        await db.commit()
        logger.debug("SqliteSessionStore: created %s", sid)
        return sid

    async def load(self, session_id: str) -> list[dict] | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT messages FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        logger.debug("SqliteSessionStore: loaded %s", session_id)
        return json.loads(row[0])

    async def save(self, session_id: str, messages: list[dict]) -> None:
        now = _utc_now_iso()
        db = await self._get_db()
        # Upsert that preserves cost_data on conflict (it's accumulator state
        # owned by ``update()`` and must not be reset by every save).
        await db.execute(
            "INSERT INTO sessions "
            "(session_id, messages, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  messages = excluded.messages, "
            "  updated_at = excluded.updated_at",
            (session_id, json.dumps(messages), now, now),
        )
        await db.commit()
        logger.debug("SqliteSessionStore: saved %s (%d messages)", session_id, len(messages))

    async def update(
        self,
        session_id: str,
        *,
        cost_data: dict | None = None,
    ) -> bool:
        db = await self._get_db()
        if cost_data is None:
            return await self.exists(session_id)
        cursor = await db.execute(
            "SELECT cost_data FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        existing = json.loads(row[0]) if row[0] else {}
        existing.update(cost_data)
        now = _utc_now_iso()
        await db.execute(
            "UPDATE sessions SET cost_data = ?, updated_at = ? "
            "WHERE session_id = ?",
            (json.dumps(existing), now, session_id),
        )
        await db.commit()
        logger.debug("SqliteSessionStore: updated %s cost_data", session_id)
        return True

    async def get_cost_data(self, session_id: str) -> dict:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT cost_data FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None or not row[0]:
            return {}
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return {}

    async def delete(self, session_id: str) -> bool:
        db = await self._get_db()
        cursor = await db.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def exists(self, session_id: str) -> bool:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        return await cursor.fetchone() is not None

    async def delete_before(self, cutoff: datetime) -> int:
        db = await self._get_db()
        cursor = await db.execute(
            "DELETE FROM sessions WHERE updated_at < ?",
            (cutoff.isoformat(),),
        )
        await db.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.debug("SqliteSessionStore: housekeeping removed %d sessions", deleted)
        return deleted

    async def update_state(self, session_id: str, **fields: Any) -> bool:
        allowed = {"pending_question", "open_tool_calls", "pending_subagent_calls",
                   "permission_scope_active", "compaction_state",
                   "checkpoint_state"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        db = await self._get_db()
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = []
        for k, v in updates.items():
            if isinstance(v, (dict, list)):
                vals.append(json.dumps(v))
            elif v is None:
                vals.append(None)
            else:
                vals.append(str(v))
        cursor = await db.execute(
            f"UPDATE sessions SET {sets}, updated_at = ? WHERE session_id = ?",
            vals + [_utc_now_iso(), session_id],
        )
        await db.commit()
        return cursor.rowcount > 0

    async def get_state(self, session_id: str) -> dict[str, Any]:
        state_cols = ["pending_question", "open_tool_calls", "pending_subagent_calls",
                      "permission_scope_active", "compaction_state",
                      "checkpoint_state",
                      "parent_session_id", "forked_at_message_id"]
        db = await self._get_db()
        cols_str = ", ".join(state_cols)
        cursor = await db.execute(
            f"SELECT {cols_str} FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {}
        result = {}
        for i, col in enumerate(state_cols):
            val = row[i]
            if col in ("open_tool_calls", "pending_subagent_calls", "compaction_state") and val:
                result[col] = json.loads(val)
            else:
                result[col] = val
        return result

    async def fork(
        self, session_id: str, from_message_index: int | None = None,
    ) -> str:
        messages = await self.load(session_id)
        if messages is None:
            raise ValueError(f"Session {session_id!r} not found")

        if from_message_index is not None:
            if from_message_index < 0 or from_message_index > len(messages):
                raise ValueError(
                    f"from_message_index {from_message_index} out of range "
                    f"(session has {len(messages)} messages)"
                )
            forked_messages = messages[:from_message_index]
        else:
            forked_messages = list(messages)

        forked_at_msg_id = None
        if forked_messages:
            forked_at_msg_id = forked_messages[-1].get("id")

        new_id = await self.create(
            parent_session_id=session_id,
            forked_at_message_id=forked_at_msg_id,
        )

        if forked_messages:
            await self.save(new_id, forked_messages)

        return new_id

    async def revert(self, session_id: str, to_message_index: int) -> None:
        messages = await self.load(session_id)
        if messages is None:
            raise ValueError(f"Session {session_id!r} not found")

        if to_message_index < 0 or to_message_index > len(messages):
            raise ValueError(
                f"to_message_index {to_message_index} out of range "
                f"(session has {len(messages)} messages)"
            )

        await self.save(session_id, messages[:to_message_index])

    async def close(self) -> None:
        if self._db is not None and not self._managed:
            await self._db.close()
            self._db = None
            self._initialized = False


class PostgresSessionStore(SessionStore):
    """Enterprise session persistence via asyncpg."""

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    messages    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    cost_data   JSONB NOT NULL DEFAULT '{}'::jsonb
)"""
    _CREATE_INDEX = (
        "CREATE INDEX IF NOT EXISTS idx_sessions_updated "
        "ON sessions (updated_at)"
    )
    _ADD_COST_DATA = (
        "ALTER TABLE sessions "
        "ADD COLUMN IF NOT EXISTS cost_data JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    _ADD_NEW_COLS = [
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS parent_session_id TEXT DEFAULT NULL",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS forked_at_message_id TEXT DEFAULT NULL",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pending_question TEXT DEFAULT NULL",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS open_tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pending_subagent_calls JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS permission_scope_active TEXT DEFAULT NULL",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS compaction_state JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS checkpoint_state TEXT DEFAULT NULL",
    ]

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: Any = None  # asyncpg.Pool
        self._initialized = False

    async def _get_pool(self) -> Any:
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self._database_url)
        if not self._initialized:
            await self._ensure_table()
        return self._pool

    async def _ensure_table(self) -> None:
        pool = self._pool
        async with pool.acquire() as conn:
            await conn.execute(self._CREATE_TABLE)
            await conn.execute(self._ADD_COST_DATA)
            for stmt in self._ADD_NEW_COLS:
                await conn.execute(stmt)
            await conn.execute(self._CREATE_INDEX)
        self._initialized = True

    async def create(
        self,
        session_id: str | None = None,
        *,
        parent_session_id: str | None = None,
        forked_at_message_id: str | None = None,
        permission_scope_active: str | None = None,
    ) -> str:
        sid = session_id or _generate_session_id()
        now = datetime.now(timezone.utc)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sessions "
                "(session_id, messages, created_at, updated_at, parent_session_id, "
                "forked_at_message_id, permission_scope_active) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                sid, json.dumps([]), now, now, parent_session_id,
                forked_at_message_id, permission_scope_active,
            )
        logger.debug("PostgresSessionStore: created %s", sid)
        return sid

    async def load(self, session_id: str) -> list[dict] | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT messages FROM sessions WHERE session_id = $1",
                session_id,
            )
        if row is None:
            return None
        logger.debug("PostgresSessionStore: loaded %s", session_id)
        messages = row["messages"]
        # asyncpg auto-decodes JSONB to Python objects
        if isinstance(messages, str):
            return json.loads(messages)
        return messages

    async def save(self, session_id: str, messages: list[dict]) -> None:
        now = datetime.now(timezone.utc)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sessions (session_id, messages, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (session_id) DO UPDATE "
                "SET messages = EXCLUDED.messages, updated_at = EXCLUDED.updated_at",
                session_id, json.dumps(messages), now, now,
            )
        logger.debug(
            "PostgresSessionStore: saved %s (%d messages)", session_id, len(messages),
        )

    async def update(
        self,
        session_id: str,
        *,
        cost_data: dict | None = None,
    ) -> bool:
        pool = await self._get_pool()
        if cost_data is None:
            return await self.exists(session_id)
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE sessions "
                "SET cost_data = cost_data || $2::jsonb, "
                "    updated_at = $3 "
                "WHERE session_id = $1",
                session_id, json.dumps(cost_data), now,
            )
        # asyncpg returns "UPDATE N"
        updated = not result.endswith("0")
        if updated:
            logger.debug("PostgresSessionStore: updated %s cost_data", session_id)
        return updated

    async def get_cost_data(self, session_id: str) -> dict:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT cost_data FROM sessions WHERE session_id = $1",
                session_id,
            )
        if row is None:
            return {}
        cost = row["cost_data"]
        # asyncpg auto-decodes JSONB to Python objects, but be defensive.
        if cost is None:
            return {}
        if isinstance(cost, str):
            try:
                return json.loads(cost)
            except (TypeError, ValueError):
                return {}
        return cost

    async def delete(self, session_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sessions WHERE session_id = $1",
                session_id,
            )
        # asyncpg returns "DELETE N"
        return not result.endswith("0")

    async def exists(self, session_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM sessions WHERE session_id = $1",
                session_id,
            )
        return row is not None

    async def delete_before(self, cutoff: datetime) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sessions WHERE updated_at < $1",
                cutoff,
            )
        # asyncpg returns "DELETE N"
        deleted = int(result.split()[-1])
        if deleted:
            logger.debug("PostgresSessionStore: housekeeping removed %d sessions", deleted)
        return deleted

    async def update_state(self, session_id: str, **fields: Any) -> bool:
        allowed = {"pending_question", "open_tool_calls", "pending_subagent_calls",
                   "permission_scope_active", "compaction_state",
                   "checkpoint_state"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        pool = await self._get_pool()
        now = datetime.now(timezone.utc)
        # Build UPDATE statement with SET clauses
        set_clauses = []
        params = []
        param_idx = 1
        for k, v in updates.items():
            if k in ("open_tool_calls", "pending_subagent_calls", "compaction_state"):
                set_clauses.append(f"{k} = ${param_idx}::jsonb")
                params.append(json.dumps(v))
            else:
                set_clauses.append(f"{k} = ${param_idx}")
                params.append(v)
            param_idx += 1
        params.append(now)
        params.append(session_id)
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE sessions SET {', '.join(set_clauses)}, "
                f"updated_at = ${param_idx} WHERE session_id = ${param_idx + 1}",
                *params,
            )
        # asyncpg returns "UPDATE N"
        updated = not result.endswith("0")
        if updated:
            logger.debug("PostgresSessionStore: updated %s state", session_id)
        return updated

    async def get_state(self, session_id: str) -> dict[str, Any]:
        state_cols = ["pending_question", "open_tool_calls", "pending_subagent_calls",
                      "permission_scope_active", "compaction_state",
                      "checkpoint_state",
                      "parent_session_id", "forked_at_message_id"]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            cols_str = ", ".join(state_cols)
            row = await conn.fetchrow(
                f"SELECT {cols_str} FROM sessions WHERE session_id = $1",
                session_id,
            )
        if row is None:
            return {}
        result = {}
        for col in state_cols:
            val = row[col]
            # asyncpg auto-decodes JSONB to Python objects
            if col in ("open_tool_calls", "pending_subagent_calls", "compaction_state"):
                if isinstance(val, str):
                    result[col] = json.loads(val)
                else:
                    result[col] = val
            else:
                result[col] = val
        return result

    async def fork(
        self, session_id: str, from_message_index: int | None = None,
    ) -> str:
        messages = await self.load(session_id)
        if messages is None:
            raise ValueError(f"Session {session_id!r} not found")

        if from_message_index is not None:
            if from_message_index < 0 or from_message_index > len(messages):
                raise ValueError(
                    f"from_message_index {from_message_index} out of range "
                    f"(session has {len(messages)} messages)"
                )
            forked_messages = messages[:from_message_index]
        else:
            forked_messages = list(messages)

        forked_at_msg_id = None
        if forked_messages:
            forked_at_msg_id = forked_messages[-1].get("id")

        new_id = await self.create(
            parent_session_id=session_id,
            forked_at_message_id=forked_at_msg_id,
        )

        if forked_messages:
            await self.save(new_id, forked_messages)

        return new_id

    async def revert(self, session_id: str, to_message_index: int) -> None:
        messages = await self.load(session_id)
        if messages is None:
            raise ValueError(f"Session {session_id!r} not found")

        if to_message_index < 0 or to_message_index > len(messages):
            raise ValueError(
                f"to_message_index {to_message_index} out of range "
                f"(session has {len(messages)} messages)"
            )

        await self.save(session_id, messages[:to_message_index])

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False


def create_session_store(
    backend: str | None,
    *,
    sqlite_path: str = "./agent.db",
    database_url: str = "",
    sqlite_connection: Any = None,
    platform_url: str = "",
    platform_token: str = "",
) -> SessionStore:
    """Create a session store from config values."""
    if backend == "sqlite":
        return SqliteSessionStore(sqlite_path, connection=sqlite_connection)
    elif backend == "postgres":
        if not database_url:
            raise ValueError("PostgresSessionStore requires database_url")
        return PostgresSessionStore(database_url)
    elif backend == "http":
        from .http import HttpSessionStore

        if not platform_url:
            raise ValueError("HttpSessionStore requires storage.platform_url")
        return HttpSessionStore(platform_url, static_token=platform_token)
    return NullSessionStore()
