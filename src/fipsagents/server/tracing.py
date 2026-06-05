"""Trace data model and persistence backends.

Traces capture one request through the agent as a tree of spans.
``TraceCollector`` (in ``collector.py``) builds them from ``StreamEvent``s;
this module provides the data model and storage.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Dedicated logger for structured trace output (used by NullTraceStore).
_trace_logger = logging.getLogger("fipsagents.tracing")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """A single operation within a trace.

    ``start_time``/``end_time`` are monotonic values for duration math;
    the parent :class:`Trace` carries wall-clock timestamps in ISO 8601.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str = ""  # e.g. "request", "step:1", "model_call", "tool:search"
    start_time: float = 0.0
    end_time: float | None = None
    status: str = "ok"  # "ok" | "error"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float | None:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time) * 1000


@dataclass
class TraceSummary:
    """Lightweight trace summary for list responses."""

    trace_id: str
    started_at: str  # ISO 8601
    ended_at: str | None
    model: str | None = None
    session_id: str | None = None
    status: str = "ok"
    duration_ms: float | None = None
    span_count: int = 0
    tool_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class Trace:
    """Complete trace with all spans."""

    trace_id: str
    started_at: str  # ISO 8601
    ended_at: str | None = None
    model: str | None = None
    session_id: str | None = None
    status: str = "ok"
    spans: list[Span] = field(default_factory=list)

    def to_summary(self) -> TraceSummary:
        """Create a lightweight summary from this trace."""
        tool_calls = sum(1 for s in self.spans if s.name.startswith("tool:"))
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        duration_ms: float | None = None

        # Aggregate token counts across all model_call spans.
        for s in self.spans:
            if s.name == "model_call":
                pt = s.attributes.get("prompt_tokens")
                ct = s.attributes.get("completion_tokens")
                if pt is not None:
                    prompt_tokens = (prompt_tokens or 0) + pt
                if ct is not None:
                    completion_tokens = (completion_tokens or 0) + ct

        # Duration from root span (first span without a parent).
        root_spans = [s for s in self.spans if s.parent_span_id is None]
        if root_spans and root_spans[0].duration_ms is not None:
            duration_ms = root_spans[0].duration_ms

        return TraceSummary(
            trace_id=self.trace_id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            model=self.model,
            session_id=self.session_id,
            status=self.status,
            duration_ms=duration_ms,
            span_count=len(self.spans),
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


def _spans_from_dicts(raw: list[dict[str, Any]]) -> list[Span]:
    """Reconstruct Span objects from serialised dicts."""
    return [
        Span(
            trace_id=s["trace_id"],
            span_id=s["span_id"],
            parent_span_id=s.get("parent_span_id"),
            name=s.get("name", ""),
            start_time=s.get("start_time", 0.0),
            end_time=s.get("end_time"),
            status=s.get("status", "ok"),
            attributes=s.get("attributes", {}),
            events=s.get("events", []),
        )
        for s in raw
    ]


def _summary_from_dict(d: dict[str, Any]) -> TraceSummary:
    """Reconstruct a TraceSummary from a serialised dict."""
    return TraceSummary(
        trace_id=d["trace_id"],
        started_at=d["started_at"],
        ended_at=d.get("ended_at"),
        model=d.get("model"),
        session_id=d.get("session_id"),
        status=d.get("status", "ok"),
        duration_ms=d.get("duration_ms"),
        span_count=d.get("span_count", 0),
        tool_calls=d.get("tool_calls", 0),
        prompt_tokens=d.get("prompt_tokens"),
        completion_tokens=d.get("completion_tokens"),
    )


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class TraceStore(ABC):
    """Pluggable trace persistence backend."""

    @abstractmethod
    async def save_trace(self, trace: Trace) -> None:
        """Persist a completed trace."""

    @abstractmethod
    async def get_trace(self, trace_id: str) -> Trace | None:
        """Retrieve a trace by ID."""

    @abstractmethod
    async def list_traces(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[TraceSummary]:
        """List recent traces (summary only)."""

    @abstractmethod
    async def delete_before(self, cutoff: datetime) -> int:
        """Remove old traces. Return count deleted."""

    async def list_traces_for_session(
        self,
        session_id: str,
        *,
        after_trace_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        """Return full traces for a session, optionally after a given trace.

        Used by state recovery to replay events since the last checkpoint.
        Default returns empty list (backward compatible).
        """
        return []

    async def close(self) -> None:
        """Release resources. Default is a no-op."""


# ---------------------------------------------------------------------------
# Null (structured logging, default)
# ---------------------------------------------------------------------------


class NullTraceStore(TraceStore):
    """No persistence -- traces are logged as structured JSON then discarded."""

    async def save_trace(self, trace: Trace) -> None:
        _trace_logger.debug(
            "trace %s: %s",
            trace.trace_id,
            json.dumps(asdict(trace), default=str),
        )

    async def get_trace(self, trace_id: str) -> Trace | None:
        return None

    async def list_traces(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[TraceSummary]:
        return []

    async def delete_before(self, cutoff: datetime) -> int:
        return 0


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteTraceStore(TraceStore):
    """Single-file trace persistence via aiosqlite."""

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS traces (
    trace_id    TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    model       TEXT,
    session_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'ok',
    spans       TEXT NOT NULL,
    summary     TEXT
)"""
    _CREATE_INDEX = (
        "CREATE INDEX IF NOT EXISTS idx_traces_started "
        "ON traces (started_at)"
    )
    _CREATE_SESSION_INDEX = (
        "CREATE INDEX IF NOT EXISTS idx_traces_session "
        "ON traces (session_id, started_at)"
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
        await db.execute(self._CREATE_INDEX)
        await db.execute(self._CREATE_SESSION_INDEX)
        await db.commit()
        self._initialized = True

    async def save_trace(self, trace: Trace) -> None:
        db = await self._get_db()
        spans_json = json.dumps([asdict(s) for s in trace.spans], default=str)
        summary_json = json.dumps(asdict(trace.to_summary()), default=str)
        await db.execute(
            "INSERT OR REPLACE INTO traces "
            "(trace_id, started_at, ended_at, model, session_id, status, spans, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace.trace_id,
                trace.started_at,
                trace.ended_at,
                trace.model,
                trace.session_id,
                trace.status,
                spans_json,
                summary_json,
            ),
        )
        await db.commit()
        logger.debug("SqliteTraceStore: saved trace %s (%d spans)", trace.trace_id, len(trace.spans))

    async def get_trace(self, trace_id: str) -> Trace | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT trace_id, started_at, ended_at, model, session_id, status, spans "
            "FROM traces WHERE trace_id = ?",
            (trace_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        spans = _spans_from_dicts(json.loads(row[6]))

        return Trace(
            trace_id=row[0],
            started_at=row[1],
            ended_at=row[2],
            model=row[3],
            session_id=row[4],
            status=row[5],
            spans=spans,
        )

    async def list_traces(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[TraceSummary]:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT summary FROM traces ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        summaries: list[TraceSummary] = []
        for (summary_json,) in rows:
            if summary_json is None:
                continue
            summaries.append(_summary_from_dict(json.loads(summary_json)))
        return summaries

    async def list_traces_for_session(
        self,
        session_id: str,
        *,
        after_trace_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        db = await self._get_db()
        if after_trace_id:
            cursor = await db.execute(
                "SELECT started_at FROM traces WHERE trace_id = ?",
                (after_trace_id,),
            )
            ref = await cursor.fetchone()
            if ref is None:
                return []
            cursor = await db.execute(
                "SELECT trace_id, started_at, ended_at, model, session_id, "
                "status, spans FROM traces "
                "WHERE session_id = ? AND started_at > ? "
                "ORDER BY started_at ASC LIMIT ?",
                (session_id, ref[0], limit),
            )
        else:
            cursor = await db.execute(
                "SELECT trace_id, started_at, ended_at, model, session_id, "
                "status, spans FROM traces "
                "WHERE session_id = ? ORDER BY started_at ASC LIMIT ?",
                (session_id, limit),
            )
        rows = await cursor.fetchall()
        traces: list[Trace] = []
        for row in rows:
            traces.append(Trace(
                trace_id=row[0],
                started_at=row[1],
                ended_at=row[2],
                model=row[3],
                session_id=row[4],
                status=row[5],
                spans=_spans_from_dicts(json.loads(row[6])),
            ))
        return traces

    async def delete_before(self, cutoff: datetime) -> int:
        db = await self._get_db()
        cursor = await db.execute(
            "DELETE FROM traces WHERE started_at < ?",
            (cutoff.isoformat(),),
        )
        await db.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.debug("SqliteTraceStore: housekeeping removed %d traces", deleted)
        return deleted

    async def close(self) -> None:
        if self._db is not None and not self._managed:
            await self._db.close()
            self._db = None
            self._initialized = False


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresTraceStore(TraceStore):
    """Enterprise trace persistence via asyncpg."""

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS traces (
    trace_id    TEXT PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    model       TEXT,
    session_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'ok',
    spans       JSONB NOT NULL,
    summary     JSONB
)"""
    _CREATE_INDEX = (
        "CREATE INDEX IF NOT EXISTS idx_traces_started "
        "ON traces (started_at)"
    )
    _CREATE_SESSION_INDEX = (
        "CREATE INDEX IF NOT EXISTS idx_traces_session "
        "ON traces (session_id, started_at)"
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
            await conn.execute(self._CREATE_INDEX)
            await conn.execute(self._CREATE_SESSION_INDEX)
        self._initialized = True

    async def save_trace(self, trace: Trace) -> None:
        pool = await self._get_pool()
        spans_json = json.dumps([asdict(s) for s in trace.spans], default=str)
        summary_json = json.dumps(asdict(trace.to_summary()), default=str)
        # Convert ISO 8601 strings to datetime for TIMESTAMPTZ columns.
        started_dt = datetime.fromisoformat(trace.started_at)
        ended_dt = (
            datetime.fromisoformat(trace.ended_at)
            if trace.ended_at else None
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO traces "
                "(trace_id, started_at, ended_at, model, session_id, "
                "status, spans, summary) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (trace_id) DO UPDATE "
                "SET ended_at = EXCLUDED.ended_at, "
                "status = EXCLUDED.status, "
                "spans = EXCLUDED.spans, summary = EXCLUDED.summary",
                trace.trace_id,
                started_dt,
                ended_dt,
                trace.model,
                trace.session_id,
                trace.status,
                spans_json,
                summary_json,
            )
        logger.debug(
            "PostgresTraceStore: saved trace %s (%d spans)",
            trace.trace_id, len(trace.spans),
        )

    async def get_trace(self, trace_id: str) -> Trace | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT trace_id, started_at, ended_at, model, "
                "session_id, status, spans "
                "FROM traces WHERE trace_id = $1",
                trace_id,
            )
        if row is None:
            return None

        spans_data = row["spans"]
        # asyncpg auto-decodes JSONB to Python objects.
        if isinstance(spans_data, str):
            spans_data = json.loads(spans_data)
        spans = _spans_from_dicts(spans_data)

        return Trace(
            trace_id=row["trace_id"],
            started_at=(
                row["started_at"].isoformat()
                if hasattr(row["started_at"], "isoformat")
                else row["started_at"]
            ),
            ended_at=(
                row["ended_at"].isoformat()
                if row["ended_at"]
                and hasattr(row["ended_at"], "isoformat")
                else row["ended_at"]
            ),
            model=row["model"],
            session_id=row["session_id"],
            status=row["status"],
            spans=spans,
        )

    async def list_traces(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[TraceSummary]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT summary FROM traces "
                "ORDER BY started_at DESC LIMIT $1 OFFSET $2",
                limit, offset,
            )

        summaries: list[TraceSummary] = []
        for row in rows:
            summary_data = row["summary"]
            if summary_data is None:
                continue
            # asyncpg auto-decodes JSONB.
            if isinstance(summary_data, str):
                summary_data = json.loads(summary_data)
            summaries.append(_summary_from_dict(summary_data))
        return summaries

    async def list_traces_for_session(
        self,
        session_id: str,
        *,
        after_trace_id: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if after_trace_id:
                ref = await conn.fetchrow(
                    "SELECT started_at FROM traces WHERE trace_id = $1",
                    after_trace_id,
                )
                if ref is None:
                    return []
                rows = await conn.fetch(
                    "SELECT trace_id, started_at, ended_at, model, "
                    "session_id, status, spans FROM traces "
                    "WHERE session_id = $1 AND started_at > $2 "
                    "ORDER BY started_at ASC LIMIT $3",
                    session_id, ref["started_at"], limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT trace_id, started_at, ended_at, model, "
                    "session_id, status, spans FROM traces "
                    "WHERE session_id = $1 "
                    "ORDER BY started_at ASC LIMIT $2",
                    session_id, limit,
                )
        traces: list[Trace] = []
        for row in rows:
            spans_data = row["spans"]
            if isinstance(spans_data, str):
                spans_data = json.loads(spans_data)
            traces.append(Trace(
                trace_id=row["trace_id"],
                started_at=(
                    row["started_at"].isoformat()
                    if hasattr(row["started_at"], "isoformat")
                    else row["started_at"]
                ),
                ended_at=(
                    row["ended_at"].isoformat()
                    if row["ended_at"]
                    and hasattr(row["ended_at"], "isoformat")
                    else row["ended_at"]
                ),
                model=row["model"],
                session_id=row["session_id"],
                status=row["status"],
                spans=_spans_from_dicts(spans_data),
            ))
        return traces

    async def delete_before(self, cutoff: datetime) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM traces WHERE started_at < $1",
                cutoff,
            )
        # asyncpg returns "DELETE N"
        deleted = int(result.split()[-1])
        if deleted:
            logger.debug(
                "PostgresTraceStore: housekeeping removed %d traces",
                deleted,
            )
        return deleted

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_trace_store(
    backend: str | None,
    *,
    sqlite_path: str = "./agent.db",
    database_url: str = "",
    sqlite_connection: Any = None,
    exporter: str | None = None,
    otel_endpoint: str | None = None,
    service_name: str = "fipsagents",
    platform_url: str = "",
    platform_token: str = "",
) -> TraceStore:
    """Create a trace store from config values."""
    if backend == "sqlite":
        inner: TraceStore = SqliteTraceStore(sqlite_path, connection=sqlite_connection)
    elif backend == "postgres":
        if not database_url:
            raise ValueError("PostgresTraceStore requires database_url")
        inner = PostgresTraceStore(database_url)
    elif backend == "http":
        from .http import HttpTraceStore

        if not platform_url:
            raise ValueError("HttpTraceStore requires storage.platform_url")
        inner = HttpTraceStore(platform_url, static_token=platform_token)
    else:
        inner = NullTraceStore()

    if exporter == "otel":
        try:
            from .otel import OTELTraceStore
        except ImportError:
            logger.warning(
                "OTEL exporter requested but opentelemetry not installed. "
                "Install with: pip install 'fipsagents[otel]'"
            )
            return inner
        return OTELTraceStore(
            endpoint=otel_endpoint,
            inner=inner,
            service_name=service_name,
        )
    return inner
