"""PostgreSQL-backed work-item store for enterprise deployments."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fipsagents.server.work_items import (
    Attempt,
    Capability,
    HandoffNote,
    WorkItem,
    WorkItemStatus,
    WorkItemStore,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return current UTC time as ISO format string."""
    return datetime.now(timezone.utc).isoformat()


class PostgresWorkItemStore(WorkItemStore):
    """Enterprise work-item persistence via asyncpg.

    Follows the same lazy-pool / ensure-table pattern as
    :class:`PostgresSessionStore`.  Atomic checkout uses
    ``pg_try_advisory_xact_lock`` to prevent concurrent checkouts
    of the same item.
    """

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'available',
    priority INTEGER NOT NULL DEFAULT 0,
    required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_tokens INTEGER,
    max_cost_usd DOUBLE PRECISION,
    max_duration_seconds INTEGER,
    assignee TEXT,
    lease_expires_at TIMESTAMPTZ,
    parent_id TEXT,
    depends_on JSONB NOT NULL DEFAULT '[]'::jsonb,
    acceptance_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    handoff_note JSONB,
    attempt_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    progress JSONB
)"""

    _CREATE_INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items (status)",
        "CREATE INDEX IF NOT EXISTS idx_work_items_parent ON work_items (parent_id)",
        (
            "CREATE INDEX IF NOT EXISTS idx_work_items_priority "
            "ON work_items (priority DESC, created_at)"
        ),
    ]

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: Any = None  # asyncpg.Pool
        self._initialized = False

    async def _get_pool(self) -> Any:
        """Lazily create the connection pool and ensure tables exist."""
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
            for stmt in self._CREATE_INDEXES:
                await conn.execute(stmt)
        self._initialized = True
        logger.debug("PostgresWorkItemStore: tables ready")

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_capabilities(caps: list[Capability]) -> str:
        return json.dumps([{"name": c.name, "value": c.value} for c in caps])

    @staticmethod
    def _serialize_handoff_note(note: HandoffNote | None) -> str | None:
        if note is None:
            return None
        return json.dumps({
            "accomplished": note.accomplished,
            "attempted": note.attempted,
            "remaining": note.remaining,
            "blockers": note.blockers,
            "artifacts": note.artifacts,
            "context": note.context,
        })

    @staticmethod
    def _serialize_attempts(attempts: list[Attempt]) -> str:
        return json.dumps([
            {
                "actor_id": a.actor_id,
                "started_at": a.started_at,
                "ended_at": a.ended_at,
                "outcome": a.outcome,
                "handoff_note": {
                    "accomplished": a.handoff_note.accomplished,
                    "attempted": a.handoff_note.attempted,
                    "remaining": a.handoff_note.remaining,
                    "blockers": a.handoff_note.blockers,
                    "artifacts": a.handoff_note.artifacts,
                    "context": a.handoff_note.context,
                } if a.handoff_note else None,
            }
            for a in attempts
        ])

    @staticmethod
    def _decode_json(val: Any) -> Any:
        """Defensively decode a JSONB value (asyncpg usually auto-decodes)."""
        if val is None:
            return None
        if isinstance(val, str):
            return json.loads(val)
        return val

    def _from_row(self, row: Any) -> WorkItem:
        """Convert an asyncpg Record to a WorkItem."""
        required_caps = self._decode_json(row["required_capabilities"])
        handoff_data = self._decode_json(row["handoff_note"])
        attempt_data = self._decode_json(row["attempt_history"])

        # asyncpg returns datetime for TIMESTAMPTZ; WorkItem uses str.
        lease_exp = row["lease_expires_at"]
        created = row["created_at"]
        updated = row["updated_at"]

        return WorkItem(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=WorkItemStatus(row["status"]),
            priority=row["priority"],
            required_capabilities=[
                Capability(name=c["name"], value=c["value"])
                for c in (required_caps or [])
            ],
            max_tokens=row["max_tokens"],
            max_cost_usd=row["max_cost_usd"],
            max_duration_seconds=row["max_duration_seconds"],
            assignee=row["assignee"],
            lease_expires_at=lease_exp.isoformat() if isinstance(lease_exp, datetime) else lease_exp,
            parent_id=row["parent_id"],
            depends_on=self._decode_json(row["depends_on"]) or [],
            acceptance_criteria=self._decode_json(row["acceptance_criteria"]) or [],
            handoff_note=HandoffNote(
                accomplished=handoff_data["accomplished"],
                attempted=handoff_data["attempted"],
                remaining=handoff_data["remaining"],
                blockers=handoff_data["blockers"],
                artifacts=handoff_data["artifacts"],
                context=handoff_data["context"],
            ) if handoff_data else None,
            attempt_history=[
                Attempt(
                    actor_id=a["actor_id"],
                    started_at=a["started_at"],
                    ended_at=a["ended_at"],
                    outcome=a["outcome"],
                    handoff_note=HandoffNote(
                        accomplished=a["handoff_note"]["accomplished"],
                        attempted=a["handoff_note"]["attempted"],
                        remaining=a["handoff_note"]["remaining"],
                        blockers=a["handoff_note"]["blockers"],
                        artifacts=a["handoff_note"]["artifacts"],
                        context=a["handoff_note"]["context"],
                    ) if a.get("handoff_note") else None,
                )
                for a in (attempt_data or [])
            ],
            created_by=row["created_by"],
            created_at=created.isoformat() if isinstance(created, datetime) else (created or ""),
            updated_at=updated.isoformat() if isinstance(updated, datetime) else (updated or ""),
        )

    # ------------------------------------------------------------------
    # ABC implementation
    # ------------------------------------------------------------------

    async def create(self, item: WorkItem) -> WorkItem:
        """Create a new work item."""
        if not item.id:
            import secrets
            import time
            item.id = f"wi_{int(time.time() * 1000)}_{secrets.token_hex(4)}"

        now = datetime.now(timezone.utc)
        if not item.created_at:
            item.created_at = now.isoformat()
        if not item.updated_at:
            item.updated_at = now.isoformat()

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO work_items
                   (id, title, description, status, priority,
                    required_capabilities, max_tokens, max_cost_usd,
                    max_duration_seconds, assignee, lease_expires_at,
                    parent_id, depends_on, acceptance_criteria,
                    handoff_note, attempt_history, created_by,
                    created_at, updated_at, progress)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
                """,
                item.id,
                item.title,
                item.description,
                item.status.value if isinstance(item.status, WorkItemStatus) else item.status,
                item.priority,
                self._serialize_capabilities(item.required_capabilities),
                item.max_tokens,
                item.max_cost_usd,
                item.max_duration_seconds,
                item.assignee,
                datetime.fromisoformat(item.lease_expires_at) if item.lease_expires_at else None,
                item.parent_id,
                json.dumps(item.depends_on),
                json.dumps(item.acceptance_criteria),
                self._serialize_handoff_note(item.handoff_note),
                self._serialize_attempts(item.attempt_history),
                item.created_by,
                datetime.fromisoformat(item.created_at) if item.created_at else now,
                datetime.fromisoformat(item.updated_at) if item.updated_at else now,
                json.dumps(item.__dict__.get("progress")) if hasattr(item, "progress") and item.__dict__.get("progress") else None,
            )

        logger.debug("PostgresWorkItemStore: created %s", item.id)
        return item

    async def list_available(
        self,
        *,
        capabilities: list[Capability] | None = None,
        max_results: int = 10,
        parent_id: str | None = None,
    ) -> list[WorkItem]:
        """List available items, optionally filtered by capabilities and parent."""
        pool = await self._get_pool()

        query = "SELECT * FROM work_items WHERE status = 'available'"
        params: list[Any] = []
        idx = 1

        if parent_id is not None:
            query += f" AND parent_id = ${idx}"
            params.append(parent_id)
            idx += 1

        query += f" ORDER BY priority DESC, created_at ASC LIMIT ${idx}"
        params.append(max_results)

        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        items = [self._from_row(row) for row in rows]

        if capabilities:
            items = [
                it for it in items
                if self._matches_capabilities(it.required_capabilities, capabilities)
            ]

        logger.debug("PostgresWorkItemStore: listed %d available items", len(items))
        return items

    def _matches_capabilities(
        self,
        required: list[Capability],
        offered: list[Capability],
    ) -> bool:
        """Check if offered capabilities satisfy all required ones."""
        if not required:
            return True
        offered_map = {cap.name: cap.value for cap in offered}
        return all(
            offered_map.get(r.name, -1) >= r.value for r in required
        )

    async def get(self, item_id: str) -> WorkItem | None:
        """Retrieve a single item by ID."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM work_items WHERE id = $1", item_id,
            )
        if row is None:
            return None
        return self._from_row(row)

    async def checkout(
        self,
        item_id: str,
        actor_id: str,
        *,
        lease_duration_seconds: int = 300,
    ) -> WorkItem:
        """Atomically check out an item using advisory locks."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                locked = await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtext($1))", item_id,
                )
                if not locked:
                    raise ValueError(
                        f"Work item {item_id!r} is locked by another checkout"
                    )

                row = await conn.fetchrow(
                    "SELECT * FROM work_items WHERE id = $1", item_id,
                )
                if row is None:
                    raise ValueError(f"Work item {item_id!r} not found")
                if row["status"] != "available":
                    raise ValueError(
                        f"Work item {item_id!r} is not available "
                        f"(status={row['status']})"
                    )

                item = self._from_row(row)

                now = datetime.now(timezone.utc)
                lease_expires = now + timedelta(seconds=lease_duration_seconds)

                item.status = WorkItemStatus.checked_out
                item.assignee = actor_id
                item.lease_expires_at = lease_expires.isoformat()
                item.updated_at = now.isoformat()

                new_attempt = Attempt(
                    actor_id=actor_id, started_at=now.isoformat(),
                )
                item.attempt_history.append(new_attempt)

                await conn.execute(
                    """UPDATE work_items SET
                       status = $1, assignee = $2, lease_expires_at = $3,
                       attempt_history = $4, updated_at = $5
                       WHERE id = $6""",
                    item.status.value,
                    actor_id,
                    lease_expires,
                    self._serialize_attempts(item.attempt_history),
                    now,
                    item_id,
                )

        logger.debug("PostgresWorkItemStore: checked out %s to %s", item_id, actor_id)
        return item

    async def renew_lease(
        self,
        item_id: str,
        actor_id: str,
        *,
        lease_duration_seconds: int = 300,
    ) -> WorkItem:
        """Extend the lease for an item already checked out by this actor."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM work_items WHERE id = $1", item_id,
            )
            if row is None:
                raise ValueError(f"Work item {item_id!r} not found")
            if row["status"] != "checked_out":
                raise ValueError(
                    f"Work item {item_id!r} is not checked out "
                    f"(status={row['status']})"
                )
            if row["assignee"] != actor_id:
                raise ValueError(
                    f"Work item {item_id!r} is checked out by "
                    f"{row['assignee']!r}, not {actor_id!r}"
                )

            item = self._from_row(row)
            now = datetime.now(timezone.utc)
            lease_expires = now + timedelta(seconds=lease_duration_seconds)

            item.lease_expires_at = lease_expires.isoformat()
            item.updated_at = now.isoformat()

            await conn.execute(
                "UPDATE work_items SET lease_expires_at = $1, updated_at = $2 WHERE id = $3",
                lease_expires, now, item_id,
            )

        logger.debug("PostgresWorkItemStore: renewed lease for %s", item_id)
        return item

    async def update_progress(
        self,
        item_id: str,
        *,
        progress: dict[str, Any],
    ) -> WorkItem:
        """Update progress metadata for an item (implicitly renews lease)."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM work_items WHERE id = $1", item_id,
            )
            if row is None:
                raise ValueError(f"Work item {item_id!r} not found")
            if row["status"] != "checked_out":
                raise ValueError(
                    f"Work item {item_id!r} is not checked out "
                    f"(status={row['status']})"
                )

            item = self._from_row(row)
            now = datetime.now(timezone.utc)
            lease_duration = item.max_duration_seconds or 300
            lease_expires = now + timedelta(seconds=lease_duration)

            item.lease_expires_at = lease_expires.isoformat()
            item.updated_at = now.isoformat()

            await conn.execute(
                """UPDATE work_items SET progress = $1,
                   lease_expires_at = $2, updated_at = $3 WHERE id = $4""",
                json.dumps(progress), lease_expires, now, item_id,
            )

        logger.debug("PostgresWorkItemStore: updated progress for %s", item_id)
        return item

    async def complete(
        self,
        item_id: str,
        *,
        result: dict[str, Any] | None = None,
        handoff_note: HandoffNote | None = None,
        review_required: bool = False,
    ) -> WorkItem:
        """Mark an item as completed or review_pending."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM work_items WHERE id = $1", item_id,
            )
            if row is None:
                raise ValueError(f"Work item {item_id!r} not found")
            if row["status"] != "checked_out":
                raise ValueError(
                    f"Work item {item_id!r} is not checked out "
                    f"(status={row['status']})"
                )

            item = self._from_row(row)
            now = datetime.now(timezone.utc)

            item.status = (
                WorkItemStatus.review_pending if review_required
                else WorkItemStatus.completed
            )
            item.assignee = None
            item.lease_expires_at = None
            item.updated_at = now.isoformat()
            item.handoff_note = handoff_note

            if item.attempt_history:
                item.attempt_history[-1].ended_at = now.isoformat()
                item.attempt_history[-1].outcome = "completed"

            await conn.execute(
                """UPDATE work_items SET
                   status = $1, assignee = $2, lease_expires_at = $3,
                   handoff_note = $4, attempt_history = $5, updated_at = $6
                   WHERE id = $7""",
                item.status.value,
                None,
                None,
                self._serialize_handoff_note(item.handoff_note),
                self._serialize_attempts(item.attempt_history),
                now,
                item_id,
            )

        logger.debug(
            "PostgresWorkItemStore: completed %s (review=%s)", item_id, review_required,
        )
        return item

    async def release(
        self,
        item_id: str,
        *,
        handoff_note: HandoffNote | None = None,
    ) -> WorkItem:
        """Release an item back to the pool with optional handoff context."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM work_items WHERE id = $1", item_id,
            )
            if row is None:
                raise ValueError(f"Work item {item_id!r} not found")
            if row["status"] != "checked_out":
                raise ValueError(
                    f"Work item {item_id!r} is not checked out "
                    f"(status={row['status']})"
                )

            item = self._from_row(row)
            now = datetime.now(timezone.utc)

            item.status = WorkItemStatus.available
            item.assignee = None
            item.lease_expires_at = None
            item.updated_at = now.isoformat()
            item.handoff_note = handoff_note

            if item.attempt_history:
                item.attempt_history[-1].ended_at = now.isoformat()
                item.attempt_history[-1].outcome = "released"

            await conn.execute(
                """UPDATE work_items SET
                   status = $1, assignee = $2, lease_expires_at = $3,
                   handoff_note = $4, attempt_history = $5, updated_at = $6
                   WHERE id = $7""",
                WorkItemStatus.available.value,
                None,
                None,
                self._serialize_handoff_note(item.handoff_note),
                self._serialize_attempts(item.attempt_history),
                now,
                item_id,
            )

        logger.debug("PostgresWorkItemStore: released %s", item_id)
        return item

    async def fail(
        self,
        item_id: str,
        *,
        error: str,
        handoff_note: HandoffNote | None = None,
        retry: bool = False,
    ) -> WorkItem:
        """Mark an item as failed. If retry is True, reset to available."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM work_items WHERE id = $1", item_id,
            )
            if row is None:
                raise ValueError(f"Work item {item_id!r} not found")

            item = self._from_row(row)
            now = datetime.now(timezone.utc)

            new_status = WorkItemStatus.available if retry else WorkItemStatus.failed
            item.status = new_status
            item.assignee = None
            item.lease_expires_at = None
            item.updated_at = now.isoformat()
            item.handoff_note = handoff_note

            if item.attempt_history:
                item.attempt_history[-1].ended_at = now.isoformat()
                item.attempt_history[-1].outcome = "failed"

            await conn.execute(
                """UPDATE work_items SET
                   status = $1, assignee = $2, lease_expires_at = $3,
                   handoff_note = $4, attempt_history = $5, updated_at = $6
                   WHERE id = $7""",
                new_status.value,
                None,
                None,
                self._serialize_handoff_note(item.handoff_note),
                self._serialize_attempts(item.attempt_history),
                now,
                item_id,
            )

        logger.debug(
            "PostgresWorkItemStore: failed %s (retry=%s, error=%s)",
            item_id, retry, error,
        )
        return item

    async def accept(self, item_id: str) -> WorkItem:
        """Accept a review_pending item, moving it to completed."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM work_items WHERE id = $1", item_id,
            )
            if row is None:
                raise ValueError(f"Work item {item_id!r} not found")
            if row["status"] != "review_pending":
                raise ValueError(
                    f"Work item {item_id!r} is not review_pending "
                    f"(status={row['status']})"
                )

            item = self._from_row(row)
            now = datetime.now(timezone.utc)
            item.status = WorkItemStatus.completed
            item.updated_at = now.isoformat()

            await conn.execute(
                "UPDATE work_items SET status = $1, updated_at = $2 WHERE id = $3",
                WorkItemStatus.completed.value, now, item_id,
            )

        logger.debug("PostgresWorkItemStore: accepted %s", item_id)
        return item

    async def reject(self, item_id: str, *, reason: str) -> WorkItem:
        """Reject a review_pending item, moving it back to available."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM work_items WHERE id = $1", item_id,
            )
            if row is None:
                raise ValueError(f"Work item {item_id!r} not found")
            if row["status"] != "review_pending":
                raise ValueError(
                    f"Work item {item_id!r} is not review_pending "
                    f"(status={row['status']})"
                )

            item = self._from_row(row)
            now = datetime.now(timezone.utc)
            item.status = WorkItemStatus.available
            item.updated_at = now.isoformat()

            if item.handoff_note is None:
                item.handoff_note = HandoffNote()
            item.handoff_note.context = (
                f"Rejected: {reason}\n\n{item.handoff_note.context}"
            )

            await conn.execute(
                """UPDATE work_items SET status = $1,
                   handoff_note = $2, updated_at = $3 WHERE id = $4""",
                WorkItemStatus.available.value,
                self._serialize_handoff_note(item.handoff_note),
                now,
                item_id,
            )

        logger.debug("PostgresWorkItemStore: rejected %s (reason=%s)", item_id, reason)
        return item

    async def expire_leases(self) -> list[WorkItem]:
        """Expire leases past their deadline."""
        pool = await self._get_pool()
        now = datetime.now(timezone.utc)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM work_items "
                "WHERE status = 'checked_out' AND lease_expires_at < $1",
                now,
            )

            expired: list[WorkItem] = []
            for row in rows:
                item = self._from_row(row)

                if item.attempt_history:
                    item.attempt_history[-1].ended_at = now.isoformat()
                    item.attempt_history[-1].outcome = "expired"

                item.status = WorkItemStatus.available
                item.assignee = None
                item.lease_expires_at = None
                item.updated_at = now.isoformat()

                await conn.execute(
                    """UPDATE work_items SET
                       status = $1, assignee = $2, lease_expires_at = $3,
                       attempt_history = $4, updated_at = $5
                       WHERE id = $6""",
                    WorkItemStatus.available.value,
                    None,
                    None,
                    self._serialize_attempts(item.attempt_history),
                    now,
                    item.id,
                )
                expired.append(item)

        if expired:
            logger.debug("PostgresWorkItemStore: expired %d leases", len(expired))
        return expired

    async def stats(self) -> dict[str, int]:
        """Aggregate counts by work-item status."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, COUNT(*) AS cnt FROM work_items GROUP BY status"
            )
        return {row["status"]: row["cnt"] for row in rows}

    async def close(self) -> None:
        """Release the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False
