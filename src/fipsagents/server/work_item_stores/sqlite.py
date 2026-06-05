"""SQLite-backed work-item store for single-replica deployments."""

from __future__ import annotations

import json
import logging
import secrets
import time
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


def _generate_item_id() -> str:
    """Generate a sortable work item ID."""
    return f"wi_{int(time.time()*1000)}_{secrets.token_hex(4)}"


def _utc_now_iso() -> str:
    """Return current UTC time as ISO format string."""
    return datetime.now(timezone.utc).isoformat()


class SqliteWorkItemStore(WorkItemStore):
    """Single-file work-item persistence via aiosqlite."""

    _CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'available',
    priority INTEGER NOT NULL DEFAULT 0,
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    max_tokens INTEGER,
    max_cost_usd REAL,
    max_duration_seconds INTEGER,
    assignee TEXT,
    lease_expires_at TEXT,
    parent_id TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    handoff_note TEXT,
    attempt_history TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    progress TEXT
)"""

    def __init__(self, db_path: str = "./agent.db", *, connection: Any = None) -> None:
        """Initialize the SQLite work-item store.

        Args:
            db_path: Path to the SQLite database file
            connection: Optional pre-existing aiosqlite connection from SqliteConnectionManager
        """
        self._db_path = db_path
        self._managed_conn = connection  # from SqliteConnectionManager
        self._own_conn: Any = None
        self._table_ready = False

    async def _get_db(self) -> Any:
        """Get the database connection, lazily opening if needed."""
        if self._managed_conn is not None:
            db = self._managed_conn
        else:
            if self._own_conn is None:
                import aiosqlite
                self._own_conn = await aiosqlite.connect(self._db_path)
            db = self._own_conn

        if not self._table_ready:
            await self._ensure_table(db)
        return db

    async def _ensure_table(self, db: Any) -> None:
        """Ensure the work_items table exists."""
        await db.execute(self._CREATE_TABLE)
        await db.commit()
        self._table_ready = True
        logger.debug("SqliteWorkItemStore: table ready")

    def _to_row(self, item: WorkItem) -> dict[str, Any]:
        """Convert a WorkItem to a dict for SQL INSERT/UPDATE."""
        return {
            "id": item.id,
            "title": item.title,
            "description": item.description,
            "status": item.status.value if isinstance(item.status, WorkItemStatus) else item.status,
            "priority": item.priority,
            "required_capabilities": json.dumps([
                {"name": c.name, "value": c.value} for c in item.required_capabilities
            ]),
            "max_tokens": item.max_tokens,
            "max_cost_usd": item.max_cost_usd,
            "max_duration_seconds": item.max_duration_seconds,
            "assignee": item.assignee,
            "lease_expires_at": item.lease_expires_at,
            "parent_id": item.parent_id,
            "depends_on": json.dumps(item.depends_on),
            "acceptance_criteria": json.dumps(item.acceptance_criteria),
            "handoff_note": json.dumps({
                "accomplished": item.handoff_note.accomplished,
                "attempted": item.handoff_note.attempted,
                "remaining": item.handoff_note.remaining,
                "blockers": item.handoff_note.blockers,
                "artifacts": item.handoff_note.artifacts,
                "context": item.handoff_note.context,
            }) if item.handoff_note else None,
            "attempt_history": json.dumps([
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
                } for a in item.attempt_history
            ]),
            "created_by": item.created_by,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "progress": json.dumps(item.__dict__.get("progress")) if hasattr(item, "progress") and item.__dict__.get("progress") else None,
        }

    def _from_row(self, row: Any) -> WorkItem:
        """Convert a database row to a WorkItem."""
        required_caps = json.loads(row["required_capabilities"])
        handoff_data = json.loads(row["handoff_note"]) if row["handoff_note"] else None
        attempt_data = json.loads(row["attempt_history"])

        return WorkItem(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=WorkItemStatus(row["status"]),
            priority=row["priority"],
            required_capabilities=[
                Capability(name=c["name"], value=c["value"]) for c in required_caps
            ],
            max_tokens=row["max_tokens"],
            max_cost_usd=row["max_cost_usd"],
            max_duration_seconds=row["max_duration_seconds"],
            assignee=row["assignee"],
            lease_expires_at=row["lease_expires_at"],
            parent_id=row["parent_id"],
            depends_on=json.loads(row["depends_on"]),
            acceptance_criteria=json.loads(row["acceptance_criteria"]),
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
                    ) if a["handoff_note"] else None,
                ) for a in attempt_data
            ],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def create(self, item: WorkItem) -> WorkItem:
        """Create a new work item."""
        if not item.id:
            item.id = _generate_item_id()

        now = _utc_now_iso()
        if not item.created_at:
            item.created_at = now
        if not item.updated_at:
            item.updated_at = now

        db = await self._get_db()
        db.row_factory = None  # Use tuple rows for INSERT

        row_data = self._to_row(item)
        columns = ", ".join(row_data.keys())
        placeholders = ", ".join("?" * len(row_data))
        values = tuple(row_data.values())

        await db.execute(
            f"INSERT INTO work_items ({columns}) VALUES ({placeholders})",
            values
        )
        await db.commit()

        logger.debug("SqliteWorkItemStore: created %s", item.id)
        return item

    async def list_available(
        self,
        *,
        capabilities: list[Capability] | None = None,
        max_results: int = 10,
        parent_id: str | None = None,
    ) -> list[WorkItem]:
        """List available items, optionally filtered by capabilities and parent."""
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        query = "SELECT * FROM work_items WHERE status = 'available'"
        params: list[Any] = []

        if parent_id is not None:
            query += " AND parent_id = ?"
            params.append(parent_id)

        query += " ORDER BY priority DESC, created_at ASC LIMIT ?"
        params.append(max_results)

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        items = [self._from_row(row) for row in rows]

        # Filter by capabilities in Python
        if capabilities:
            filtered = []
            for item in items:
                if self._matches_capabilities(item.required_capabilities, capabilities):
                    filtered.append(item)
            items = filtered

        logger.debug("SqliteWorkItemStore: listed %d available items", len(items))
        return items

    def _matches_capabilities(
        self,
        required: list[Capability],
        offered: list[Capability],
    ) -> bool:
        """Check if offered capabilities satisfy all required capabilities.

        For each required capability, there must be an offered capability
        with the same name and value >= required value.
        """
        if not required:
            return True

        offered_map = {cap.name: cap.value for cap in offered}

        for req_cap in required:
            offered_value = offered_map.get(req_cap.name)
            if offered_value is None or offered_value < req_cap.value:
                return False

        return True

    async def get(self, item_id: str) -> WorkItem | None:
        """Retrieve a single item by ID."""
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()

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
        """Check out an item, setting assignee and lease expiry."""
        db = await self._get_db()

        import aiosqlite

        # Use BEGIN IMMEDIATE for atomic locking
        await db.execute("BEGIN IMMEDIATE")

        try:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM work_items WHERE id = ?",
                (item_id,)
            )
            row = await cursor.fetchone()

            if row is None:
                await db.rollback()
                raise ValueError(f"Work item {item_id!r} not found")

            if row["status"] != "available":
                await db.rollback()
                raise ValueError(
                    f"Work item {item_id!r} is not available (status={row['status']})"
                )

            item = self._from_row(row)

            # Update status and lease
            now = datetime.now(timezone.utc)
            lease_expires = now + timedelta(seconds=lease_duration_seconds)

            item.status = WorkItemStatus.checked_out
            item.assignee = actor_id
            item.lease_expires_at = lease_expires.isoformat()
            item.updated_at = now.isoformat()

            # Add new attempt
            new_attempt = Attempt(
                actor_id=actor_id,
                started_at=now.isoformat(),
            )
            item.attempt_history.append(new_attempt)

            # Update database
            row_data = self._to_row(item)
            await db.execute(
                """UPDATE work_items SET
                   status = ?, assignee = ?, lease_expires_at = ?,
                   attempt_history = ?, updated_at = ?
                   WHERE id = ?""",
                (row_data["status"], row_data["assignee"], row_data["lease_expires_at"],
                 row_data["attempt_history"], row_data["updated_at"], item_id)
            )

            await db.commit()
            logger.debug("SqliteWorkItemStore: checked out %s to %s", item_id, actor_id)
            return item

        except Exception:
            await db.rollback()
            raise

    async def renew_lease(
        self,
        item_id: str,
        actor_id: str,
        *,
        lease_duration_seconds: int = 300,
    ) -> WorkItem:
        """Extend the lease for an item already checked out by this actor."""
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            raise ValueError(f"Work item {item_id!r} not found")

        if row["status"] != "checked_out":
            raise ValueError(
                f"Work item {item_id!r} is not checked out (status={row['status']})"
            )

        if row["assignee"] != actor_id:
            raise ValueError(
                f"Work item {item_id!r} is checked out by {row['assignee']!r}, not {actor_id!r}"
            )

        item = self._from_row(row)

        now = datetime.now(timezone.utc)
        lease_expires = now + timedelta(seconds=lease_duration_seconds)

        item.lease_expires_at = lease_expires.isoformat()
        item.updated_at = now.isoformat()

        await db.execute(
            "UPDATE work_items SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
            (item.lease_expires_at, item.updated_at, item_id)
        )
        await db.commit()

        logger.debug("SqliteWorkItemStore: renewed lease for %s", item_id)
        return item

    async def update_progress(
        self,
        item_id: str,
        *,
        progress: dict[str, Any],
    ) -> WorkItem:
        """Update progress metadata for an item."""
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            raise ValueError(f"Work item {item_id!r} not found")

        if row["status"] != "checked_out":
            raise ValueError(
                f"Work item {item_id!r} is not checked out (status={row['status']})"
            )

        item = self._from_row(row)

        # Implicitly renew lease (use max_duration_seconds if set, else default 300s)
        now = datetime.now(timezone.utc)
        lease_duration = item.max_duration_seconds or 300
        lease_expires = now + timedelta(seconds=lease_duration)

        item.lease_expires_at = lease_expires.isoformat()
        item.updated_at = now.isoformat()

        await db.execute(
            "UPDATE work_items SET progress = ?, lease_expires_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(progress), item.lease_expires_at, item.updated_at, item_id)
        )
        await db.commit()

        logger.debug("SqliteWorkItemStore: updated progress for %s", item_id)
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
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            raise ValueError(f"Work item {item_id!r} not found")

        if row["status"] != "checked_out":
            raise ValueError(
                f"Work item {item_id!r} is not checked out (status={row['status']})"
            )

        item = self._from_row(row)

        now = _utc_now_iso()

        # Update status
        item.status = WorkItemStatus.review_pending if review_required else WorkItemStatus.completed
        item.assignee = None
        item.lease_expires_at = None
        item.updated_at = now
        item.handoff_note = handoff_note

        # Update attempt history: mark current attempt as completed
        if item.attempt_history:
            item.attempt_history[-1].ended_at = now
            item.attempt_history[-1].outcome = "completed"

        row_data = self._to_row(item)
        await db.execute(
            """UPDATE work_items SET
               status = ?, assignee = ?, lease_expires_at = ?,
               handoff_note = ?, attempt_history = ?, updated_at = ?
               WHERE id = ?""",
            (row_data["status"], row_data["assignee"], row_data["lease_expires_at"],
             row_data["handoff_note"], row_data["attempt_history"], row_data["updated_at"], item_id)
        )
        await db.commit()

        logger.debug("SqliteWorkItemStore: completed %s (review=%s)", item_id, review_required)
        return item

    async def release(
        self,
        item_id: str,
        *,
        handoff_note: HandoffNote | None = None,
    ) -> WorkItem:
        """Release an item back to the pool with optional handoff context."""
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            raise ValueError(f"Work item {item_id!r} not found")

        if row["status"] != "checked_out":
            raise ValueError(
                f"Work item {item_id!r} is not checked out (status={row['status']})"
            )

        item = self._from_row(row)

        now = _utc_now_iso()

        item.status = WorkItemStatus.available
        item.assignee = None
        item.lease_expires_at = None
        item.updated_at = now
        item.handoff_note = handoff_note

        # Update attempt history
        if item.attempt_history:
            item.attempt_history[-1].ended_at = now
            item.attempt_history[-1].outcome = "released"

        row_data = self._to_row(item)
        await db.execute(
            """UPDATE work_items SET
               status = ?, assignee = ?, lease_expires_at = ?,
               handoff_note = ?, attempt_history = ?, updated_at = ?
               WHERE id = ?""",
            (row_data["status"], row_data["assignee"], row_data["lease_expires_at"],
             row_data["handoff_note"], row_data["attempt_history"], row_data["updated_at"], item_id)
        )
        await db.commit()

        logger.debug("SqliteWorkItemStore: released %s", item_id)
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
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            raise ValueError(f"Work item {item_id!r} not found")

        item = self._from_row(row)

        now = _utc_now_iso()

        item.status = WorkItemStatus.available if retry else WorkItemStatus.failed
        item.assignee = None
        item.lease_expires_at = None
        item.updated_at = now
        item.handoff_note = handoff_note

        # Update attempt history
        if item.attempt_history:
            item.attempt_history[-1].ended_at = now
            item.attempt_history[-1].outcome = "failed"

        row_data = self._to_row(item)
        await db.execute(
            """UPDATE work_items SET
               status = ?, assignee = ?, lease_expires_at = ?,
               handoff_note = ?, attempt_history = ?, updated_at = ?
               WHERE id = ?""",
            (row_data["status"], row_data["assignee"], row_data["lease_expires_at"],
             row_data["handoff_note"], row_data["attempt_history"], row_data["updated_at"], item_id)
        )
        await db.commit()

        logger.debug("SqliteWorkItemStore: failed %s (retry=%s, error=%s)", item_id, retry, error)
        return item

    async def accept(self, item_id: str) -> WorkItem:
        """Accept a review_pending item, moving it to completed."""
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            raise ValueError(f"Work item {item_id!r} not found")

        if row["status"] != "review_pending":
            raise ValueError(
                f"Work item {item_id!r} is not review_pending (status={row['status']})"
            )

        item = self._from_row(row)

        now = _utc_now_iso()
        item.status = WorkItemStatus.completed
        item.updated_at = now

        await db.execute(
            "UPDATE work_items SET status = ?, updated_at = ? WHERE id = ?",
            (item.status.value, item.updated_at, item_id)
        )
        await db.commit()

        logger.debug("SqliteWorkItemStore: accepted %s", item_id)
        return item

    async def reject(self, item_id: str, *, reason: str) -> WorkItem:
        """Reject a review_pending item, moving it back to available."""
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (item_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            raise ValueError(f"Work item {item_id!r} not found")

        if row["status"] != "review_pending":
            raise ValueError(
                f"Work item {item_id!r} is not review_pending (status={row['status']})"
            )

        item = self._from_row(row)

        now = _utc_now_iso()
        item.status = WorkItemStatus.available
        item.updated_at = now

        # Add rejection reason to handoff_note context
        if item.handoff_note is None:
            item.handoff_note = HandoffNote()
        item.handoff_note.context = f"Rejected: {reason}\n\n{item.handoff_note.context}"

        row_data = self._to_row(item)
        await db.execute(
            "UPDATE work_items SET status = ?, handoff_note = ?, updated_at = ? WHERE id = ?",
            (row_data["status"], row_data["handoff_note"], row_data["updated_at"], item_id)
        )
        await db.commit()

        logger.debug("SqliteWorkItemStore: rejected %s (reason=%s)", item_id, reason)
        return item

    async def expire_leases(self) -> list[WorkItem]:
        """Expire leases past their deadline."""
        db = await self._get_db()

        import aiosqlite
        db.row_factory = aiosqlite.Row

        now = _utc_now_iso()

        cursor = await db.execute(
            "SELECT * FROM work_items WHERE status = 'checked_out' AND lease_expires_at < ?",
            (now,)
        )
        rows = await cursor.fetchall()

        expired = []

        for row in rows:
            item = self._from_row(row)

            # Preserve handoff note from the attempt
            if item.attempt_history:
                item.attempt_history[-1].ended_at = now
                item.attempt_history[-1].outcome = "expired"

            item.status = WorkItemStatus.available
            item.assignee = None
            item.lease_expires_at = None
            item.updated_at = now

            row_data = self._to_row(item)
            await db.execute(
                """UPDATE work_items SET
                   status = ?, assignee = ?, lease_expires_at = ?,
                   attempt_history = ?, updated_at = ?
                   WHERE id = ?""",
                (row_data["status"], row_data["assignee"], row_data["lease_expires_at"],
                 row_data["attempt_history"], row_data["updated_at"], item.id)
            )

            expired.append(item)

        await db.commit()

        if expired:
            logger.debug("SqliteWorkItemStore: expired %d leases", len(expired))

        return expired

    async def stats(self) -> dict[str, int]:
        """Aggregate counts by work-item status."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT status, COUNT(*) FROM work_items GROUP BY status"
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def close(self) -> None:
        """Release resources."""
        if self._own_conn is not None:
            await self._own_conn.close()
            self._own_conn = None
            self._table_ready = False
