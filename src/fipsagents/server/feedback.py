"""Feedback persistence backends.

Stores and retrieves user feedback (thumbs-up/down, comments, corrections)
linked to traces and sessions.  The server exposes REST endpoints for
submitting and querying feedback; this module provides the data model and
pluggable storage.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _generate_feedback_id() -> str:
    return f"fb_{uuid.uuid4().hex[:16]}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FeedbackRecord:
    """A single piece of user feedback tied to a trace.

    ``user_id`` is the gateway-issued ``X-Auth-Subject`` header value:
    a stable per-user identifier or the literal string ``"anonymous"``
    when the gateway is configured in anonymous mode. It is gateway-issued
    so a client cannot spoof it — see gateway-template#21 v1.
    """

    feedback_id: str
    trace_id: str
    session_id: str | None
    rating: int  # 1 or -1
    comment: str | None
    correction: str | None
    model_id: str | None
    latency_ms: float | None
    turn_index: int | None
    agent_type: str | None
    created_at: str  # ISO 8601 UTC
    user_id: str = "anonymous"


@dataclass
class FeedbackStats:
    """Aggregated thumbs-up/down counts for a time window."""

    window_start: str
    window_end: str
    agent_type: str | None
    thumbs_up: int
    thumbs_down: int
    total: int


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class FeedbackStore(ABC):
    """Pluggable feedback persistence backend."""

    @abstractmethod
    async def add(self, record: FeedbackRecord) -> str:
        """Persist a feedback record. Returns the feedback_id."""

    @abstractmethod
    async def get(self, feedback_id: str) -> FeedbackRecord | None:
        """Retrieve a single feedback record by ID."""

    @abstractmethod
    async def query(
        self,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FeedbackRecord]:
        """Query feedback with optional filters."""

    @abstractmethod
    async def stats(
        self,
        *,
        window: str = "day",
        agent_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[FeedbackStats]:
        """Aggregated thumbs-up/down grouped by time window and agent_type."""

    @abstractmethod
    async def update(
        self,
        feedback_id: str,
        *,
        rating: int | None = None,
        comment: str | None = None,
        correction: str | None = None,
    ) -> FeedbackRecord | None:
        """Mutate fields on an existing record. Pass ``None`` to leave a
        field unchanged. Returns the updated record, or ``None`` if no
        record with ``feedback_id`` exists.
        """

    @abstractmethod
    async def delete_before(self, cutoff: datetime) -> int:
        """Remove old feedback records. Return count deleted."""

    async def close(self) -> None:
        """Release resources. Default is a no-op."""


# ---------------------------------------------------------------------------
# Null (default, no persistence)
# ---------------------------------------------------------------------------


class NullFeedbackStore(FeedbackStore):
    """No persistence -- feedback is logged then discarded."""

    async def add(self, record: FeedbackRecord) -> str:
        logger.debug("NullFeedbackStore: add %s (discarded)", record.feedback_id)
        return record.feedback_id

    async def get(self, feedback_id: str) -> FeedbackRecord | None:
        return None

    async def query(
        self,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FeedbackRecord]:
        return []

    async def stats(
        self,
        *,
        window: str = "day",
        agent_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[FeedbackStats]:
        return []

    async def update(
        self,
        feedback_id: str,
        *,
        rating: int | None = None,
        comment: str | None = None,
        correction: str | None = None,
    ) -> FeedbackRecord | None:
        return None

    async def delete_before(self, cutoff: datetime) -> int:
        return 0


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def _row_to_record(row: tuple) -> FeedbackRecord:
    """Construct a FeedbackRecord from a SQLite row."""
    return FeedbackRecord(
        feedback_id=row[0],
        trace_id=row[1],
        session_id=row[2],
        rating=row[3],
        comment=row[4],
        correction=row[5],
        model_id=row[6],
        latency_ms=row[7],
        turn_index=row[8],
        agent_type=row[9],
        created_at=row[10],
        user_id=row[11] if len(row) > 11 and row[11] is not None else "anonymous",
    )


def _compute_window_end(window_start: str, window: str) -> str:
    """Compute the end of a time window given its start and bucket size."""
    dt = datetime.fromisoformat(window_start)
    if window == "hour":
        dt += timedelta(hours=1)
    elif window == "week":
        dt += timedelta(weeks=1)
    else:  # day
        dt += timedelta(days=1)
    return dt.isoformat()


class SqliteFeedbackStore(FeedbackStore):
    """Single-file feedback persistence via aiosqlite."""

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id  TEXT PRIMARY KEY,
    trace_id     TEXT NOT NULL,
    session_id   TEXT,
    rating       INTEGER NOT NULL,
    comment      TEXT,
    correction   TEXT,
    model_id     TEXT,
    latency_ms   REAL,
    turn_index   INTEGER,
    agent_type   TEXT,
    created_at   TEXT NOT NULL,
    user_id      TEXT NOT NULL DEFAULT 'anonymous'
)"""

    _CREATE_INDEXES = (
        "CREATE INDEX IF NOT EXISTS idx_feedback_trace_id ON feedback (trace_id)",
        "CREATE INDEX IF NOT EXISTS idx_feedback_session_id ON feedback (session_id)",
        "CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback (created_at)",
        "CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback (user_id)",
    )

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
        # Lightweight migration: pre-cutover databases lack the user_id
        # column. ALTER TABLE ADD COLUMN is cheap and idempotent via the
        # PRAGMA check.
        await self._migrate_user_id_column(db)
        for idx_sql in self._CREATE_INDEXES:
            await db.execute(idx_sql)
        await db.commit()
        self._initialized = True

    async def _migrate_user_id_column(self, db: Any) -> None:
        cursor = await db.execute("PRAGMA table_info(feedback)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "user_id" not in cols:
            await db.execute(
                "ALTER TABLE feedback ADD COLUMN user_id TEXT NOT NULL DEFAULT 'anonymous'"
            )

    async def add(self, record: FeedbackRecord) -> str:
        db = await self._get_db()
        await db.execute(
            "INSERT INTO feedback "
            "(feedback_id, trace_id, session_id, rating, comment, correction, "
            "model_id, latency_ms, turn_index, agent_type, created_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.feedback_id,
                record.trace_id,
                record.session_id,
                record.rating,
                record.comment,
                record.correction,
                record.model_id,
                record.latency_ms,
                record.turn_index,
                record.agent_type,
                record.created_at,
                record.user_id,
            ),
        )
        await db.commit()
        logger.debug("SqliteFeedbackStore: added %s", record.feedback_id)
        return record.feedback_id

    async def get(self, feedback_id: str) -> FeedbackRecord | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT feedback_id, trace_id, session_id, rating, comment, "
            "correction, model_id, latency_ms, turn_index, agent_type, created_at, "
            "user_id "
            "FROM feedback WHERE feedback_id = ?",
            (feedback_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def query(
        self,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FeedbackRecord]:
        db = await self._get_db()
        clauses: list[str] = []
        params: list[Any] = []

        if trace_id is not None:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("created_at < ?")
            params.append(until.isoformat())

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT feedback_id, trace_id, session_id, rating, comment, "
            "correction, model_id, latency_ms, turn_index, agent_type, created_at, "
            "user_id "
            f"FROM feedback{where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]

    async def stats(
        self,
        *,
        window: str = "day",
        agent_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[FeedbackStats]:
        db = await self._get_db()

        window_exprs = {
            "hour": "strftime('%Y-%m-%dT%H:00:00Z', created_at)",
            "day": "strftime('%Y-%m-%dT00:00:00Z', created_at)",
            "week": "strftime('%Y-%m-%dT00:00:00Z', date(created_at, 'weekday 0', '-6 days'))",
        }
        window_expr = window_exprs.get(window, window_exprs["day"])

        clauses: list[str] = []
        params: list[Any] = []

        if agent_type is not None:
            clauses.append("agent_type = ?")
            params.append(agent_type)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("created_at < ?")
            params.append(until.isoformat())

        and_clauses = (" AND " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT {window_expr} AS window_start, agent_type, "
            "SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS thumbs_up, "
            "SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS thumbs_down, "
            "COUNT(*) AS total "
            f"FROM feedback WHERE 1=1{and_clauses} "
            "GROUP BY window_start, agent_type "
            "ORDER BY window_start ASC"
        )

        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [
            FeedbackStats(
                window_start=r[0],
                window_end=_compute_window_end(r[0], window),
                agent_type=r[1],
                thumbs_up=r[2],
                thumbs_down=r[3],
                total=r[4],
            )
            for r in rows
        ]

    async def update(
        self,
        feedback_id: str,
        *,
        rating: int | None = None,
        comment: str | None = None,
        correction: str | None = None,
    ) -> FeedbackRecord | None:
        if rating is None and comment is None and correction is None:
            return await self.get(feedback_id)
        db = await self._get_db()
        sets: list[str] = []
        params: list[Any] = []
        if rating is not None:
            sets.append("rating = ?")
            params.append(rating)
        if comment is not None:
            sets.append("comment = ?")
            params.append(comment)
        if correction is not None:
            sets.append("correction = ?")
            params.append(correction)
        params.append(feedback_id)
        cursor = await db.execute(
            f"UPDATE feedback SET {', '.join(sets)} WHERE feedback_id = ?",
            params,
        )
        await db.commit()
        if cursor.rowcount == 0:
            return None
        return await self.get(feedback_id)

    async def delete_before(self, cutoff: datetime) -> int:
        db = await self._get_db()
        cursor = await db.execute(
            "DELETE FROM feedback WHERE created_at < ?",
            (cutoff.isoformat(),),
        )
        await db.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.debug("SqliteFeedbackStore: housekeeping removed %d records", deleted)
        return deleted

    async def close(self) -> None:
        if self._db is not None and not self._managed:
            await self._db.close()
            self._db = None
            self._initialized = False


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def _pg_row_to_record(row: Any) -> FeedbackRecord:
    """Construct a FeedbackRecord from an asyncpg Record."""
    created = row["created_at"]
    # row.get() not available on asyncpg.Record — use try/except.
    try:
        user_id = row["user_id"] or "anonymous"
    except (KeyError, IndexError):
        user_id = "anonymous"
    return FeedbackRecord(
        feedback_id=row["feedback_id"],
        trace_id=row["trace_id"],
        session_id=row["session_id"],
        rating=row["rating"],
        comment=row["comment"],
        correction=row["correction"],
        model_id=row["model_id"],
        latency_ms=row["latency_ms"],
        turn_index=row["turn_index"],
        agent_type=row["agent_type"],
        created_at=created.isoformat() if hasattr(created, "isoformat") else created,
        user_id=user_id,
    )


class PostgresFeedbackStore(FeedbackStore):
    """Enterprise feedback persistence via asyncpg."""

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id  TEXT PRIMARY KEY,
    trace_id     TEXT NOT NULL,
    session_id   TEXT,
    rating       INTEGER NOT NULL CHECK (rating IN (1, -1)),
    comment      TEXT,
    correction   TEXT,
    model_id     TEXT,
    latency_ms   DOUBLE PRECISION,
    turn_index   INTEGER,
    agent_type   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id      TEXT NOT NULL DEFAULT 'anonymous'
)"""

    # ALTER ... ADD COLUMN IF NOT EXISTS handles pre-cutover databases.
    _MIGRATE_USER_ID = (
        "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS "
        "user_id TEXT NOT NULL DEFAULT 'anonymous'"
    )

    _CREATE_INDEXES = (
        "CREATE INDEX IF NOT EXISTS idx_feedback_trace_id ON feedback (trace_id)",
        "CREATE INDEX IF NOT EXISTS idx_feedback_session_id ON feedback (session_id)",
        "CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback (created_at)",
        "CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback (user_id)",
    )

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
        async with self._pool.acquire() as conn:
            await conn.execute(self._CREATE_TABLE)
            await conn.execute(self._MIGRATE_USER_ID)
            for idx_sql in self._CREATE_INDEXES:
                await conn.execute(idx_sql)
        self._initialized = True

    async def add(self, record: FeedbackRecord) -> str:
        pool = await self._get_pool()
        created_dt = datetime.fromisoformat(record.created_at)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO feedback "
                "(feedback_id, trace_id, session_id, rating, comment, correction, "
                "model_id, latency_ms, turn_index, agent_type, created_at, user_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                record.feedback_id,
                record.trace_id,
                record.session_id,
                record.rating,
                record.comment,
                record.correction,
                record.model_id,
                record.latency_ms,
                record.turn_index,
                record.agent_type,
                created_dt,
                record.user_id,
            )
        logger.debug("PostgresFeedbackStore: added %s", record.feedback_id)
        return record.feedback_id

    async def get(self, feedback_id: str) -> FeedbackRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT feedback_id, trace_id, session_id, rating, comment, "
                "correction, model_id, latency_ms, turn_index, agent_type, created_at, "
                "user_id "
                "FROM feedback WHERE feedback_id = $1",
                feedback_id,
            )
        if row is None:
            return None
        return _pg_row_to_record(row)

    async def query(
        self,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FeedbackRecord]:
        pool = await self._get_pool()
        clauses: list[str] = []
        params: list[Any] = []
        idx = 1

        if trace_id is not None:
            clauses.append(f"trace_id = ${idx}")
            params.append(trace_id)
            idx += 1
        if session_id is not None:
            clauses.append(f"session_id = ${idx}")
            params.append(session_id)
            idx += 1
        if user_id is not None:
            clauses.append(f"user_id = ${idx}")
            params.append(user_id)
            idx += 1
        if since is not None:
            clauses.append(f"created_at >= ${idx}")
            params.append(since)
            idx += 1
        if until is not None:
            clauses.append(f"created_at < ${idx}")
            params.append(until)
            idx += 1

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT feedback_id, trace_id, session_id, rating, comment, "
            "correction, model_id, latency_ms, turn_index, agent_type, created_at, "
            "user_id "
            f"FROM feedback{where} "
            f"ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
        )
        params.extend([limit, offset])

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [_pg_row_to_record(r) for r in rows]

    async def stats(
        self,
        *,
        window: str = "day",
        agent_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[FeedbackStats]:
        pool = await self._get_pool()
        clauses: list[str] = []
        params: list[Any] = []
        # $1 is reserved for the date_trunc interval
        idx = 2

        if agent_type is not None:
            clauses.append(f"agent_type = ${idx}")
            params.append(agent_type)
            idx += 1
        if since is not None:
            clauses.append(f"created_at >= ${idx}")
            params.append(since)
            idx += 1
        if until is not None:
            clauses.append(f"created_at < ${idx}")
            params.append(until)
            idx += 1

        and_clauses = (" AND " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT date_trunc($1, created_at) AS window_start, agent_type, "
            "SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS thumbs_up, "
            "SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS thumbs_down, "
            "COUNT(*) AS total "
            f"FROM feedback WHERE 1=1{and_clauses} "
            "GROUP BY window_start, agent_type "
            "ORDER BY window_start ASC"
        )

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, window, *params)

        return [
            FeedbackStats(
                window_start=r["window_start"].isoformat(),
                window_end=(
                    r["window_start"] + _pg_window_delta(window)
                ).isoformat(),
                agent_type=r["agent_type"],
                thumbs_up=r["thumbs_up"],
                thumbs_down=r["thumbs_down"],
                total=r["total"],
            )
            for r in rows
        ]

    async def update(
        self,
        feedback_id: str,
        *,
        rating: int | None = None,
        comment: str | None = None,
        correction: str | None = None,
    ) -> FeedbackRecord | None:
        if rating is None and comment is None and correction is None:
            return await self.get(feedback_id)
        pool = await self._get_pool()
        sets: list[str] = []
        params: list[Any] = []
        idx = 1
        if rating is not None:
            sets.append(f"rating = ${idx}")
            params.append(rating)
            idx += 1
        if comment is not None:
            sets.append(f"comment = ${idx}")
            params.append(comment)
            idx += 1
        if correction is not None:
            sets.append(f"correction = ${idx}")
            params.append(correction)
            idx += 1
        params.append(feedback_id)
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE feedback SET {', '.join(sets)} WHERE feedback_id = ${idx}",
                *params,
            )
        # asyncpg returns "UPDATE N"
        if int(result.split()[-1]) == 0:
            return None
        return await self.get(feedback_id)

    async def delete_before(self, cutoff: datetime) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM feedback WHERE created_at < $1",
                cutoff,
            )
        # asyncpg returns "DELETE N"
        deleted = int(result.split()[-1])
        if deleted:
            logger.debug(
                "PostgresFeedbackStore: housekeeping removed %d records", deleted,
            )
        return deleted

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False


def _pg_window_delta(window: str) -> timedelta:
    """Return the timedelta for a Postgres date_trunc window bucket."""
    if window == "hour":
        return timedelta(hours=1)
    if window == "week":
        return timedelta(weeks=1)
    return timedelta(days=1)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_feedback_store(
    backend: str | None,
    *,
    sqlite_path: str = "./agent.db",
    database_url: str = "",
    sqlite_connection: Any = None,
    platform_url: str = "",
    platform_token: str = "",
) -> FeedbackStore:
    """Create a feedback store from config values."""
    if backend == "sqlite":
        return SqliteFeedbackStore(sqlite_path, connection=sqlite_connection)
    elif backend == "postgres":
        if not database_url:
            raise ValueError("PostgresFeedbackStore requires database_url")
        return PostgresFeedbackStore(database_url)
    elif backend == "http":
        from .http import HttpFeedbackStore

        if not platform_url:
            raise ValueError("HttpFeedbackStore requires storage.platform_url")
        return HttpFeedbackStore(platform_url, static_token=platform_token)
    return NullFeedbackStore()
