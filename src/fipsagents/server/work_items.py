"""Work-item pool coordination.

Provides lease-based checkout, capability matching, budget-aware allocation,
and handoff mechanics for multi-agent work distribution. Agents check out
items, work on them within a lease window, and either complete or release
them back to the pool. The ABC supports multiple backends (null, SQLite,
Postgres) following the same pattern as sessions/traces/files.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WorkItemStatus(str, Enum):
    """Lifecycle state of a work item."""
    available = "available"
    checked_out = "checked_out"
    completed = "completed"
    failed = "failed"
    review_pending = "review_pending"
    blocked = "blocked"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Capability:
    """A skill or attribute required or offered for work-item matching."""
    name: str
    value: float = 1.0


@dataclass
class HandoffNote:
    """Structured handoff context from a prior attempt."""
    accomplished: list[str] = field(default_factory=list)
    attempted: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    context: str = ""


@dataclass
class Attempt:
    """Record of a single agent attempt on a work item."""
    actor_id: str
    started_at: str
    ended_at: str | None = None
    outcome: str | None = None
    handoff_note: HandoffNote | None = None


@dataclass
class WorkItem:
    """A discrete unit of work in the pool."""
    id: str
    title: str
    description: str = ""
    status: WorkItemStatus = WorkItemStatus.available
    priority: int = 0
    required_capabilities: list[Capability] = field(default_factory=list)
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_duration_seconds: int | None = None
    assignee: str | None = None
    lease_expires_at: str | None = None
    parent_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    handoff_note: HandoffNote | None = None
    attempt_history: list[Attempt] = field(default_factory=list)
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class WorkItemStore(ABC):
    """Pluggable work-item persistence backend."""

    @abstractmethod
    async def create(self, item: WorkItem) -> WorkItem:
        """Create a new work item. Returns the created item."""

    @abstractmethod
    async def list_available(
        self,
        *,
        capabilities: list[Capability] | None = None,
        max_results: int = 10,
        parent_id: str | None = None,
    ) -> list[WorkItem]:
        """List available items, optionally filtered by capabilities and parent."""

    @abstractmethod
    async def get(self, item_id: str) -> WorkItem | None:
        """Retrieve a single item by ID. Returns None if not found."""

    @abstractmethod
    async def checkout(
        self,
        item_id: str,
        actor_id: str,
        *,
        lease_duration_seconds: int = 300,
    ) -> WorkItem:
        """Check out an item, setting assignee and lease expiry."""

    @abstractmethod
    async def renew_lease(
        self,
        item_id: str,
        actor_id: str,
        *,
        lease_duration_seconds: int = 300,
    ) -> WorkItem:
        """Extend the lease for an item already checked out by this actor."""

    @abstractmethod
    async def update_progress(
        self,
        item_id: str,
        *,
        progress: dict[str, Any],
    ) -> WorkItem:
        """Update progress metadata for an item. Returns the updated item."""

    @abstractmethod
    async def complete(
        self,
        item_id: str,
        *,
        result: dict[str, Any] | None = None,
        handoff_note: HandoffNote | None = None,
        review_required: bool = False,
    ) -> WorkItem:
        """Mark an item as completed or review_pending."""

    @abstractmethod
    async def release(
        self,
        item_id: str,
        *,
        handoff_note: HandoffNote | None = None,
    ) -> WorkItem:
        """Release an item back to the pool with optional handoff context."""

    @abstractmethod
    async def fail(
        self,
        item_id: str,
        *,
        error: str,
        handoff_note: HandoffNote | None = None,
        retry: bool = False,
    ) -> WorkItem:
        """Mark an item as failed. If retry is True, reset to available."""

    @abstractmethod
    async def accept(self, item_id: str) -> WorkItem:
        """Accept a review_pending item, moving it to completed."""

    @abstractmethod
    async def reject(self, item_id: str, *, reason: str) -> WorkItem:
        """Reject a review_pending item, moving it back to available."""

    async def stats(self) -> dict[str, int]:
        """Aggregate counts by work-item status. Returns ``{status_name: count}``."""
        return {}

    async def expire_leases(self) -> list[WorkItem]:
        """Expire leases past their deadline. Returns expired items."""
        return []

    async def close(self) -> None:
        """Release resources. Default is a no-op."""


# ---------------------------------------------------------------------------
# Null backend
# ---------------------------------------------------------------------------


class NullWorkItemStore(WorkItemStore):
    """No persistence — all operations are no-ops."""

    async def create(self, item: WorkItem) -> WorkItem:
        logger.debug("NullWorkItemStore: discarded create for %s", item.id)
        return item

    async def list_available(
        self,
        *,
        capabilities: list[Capability] | None = None,
        max_results: int = 10,
        parent_id: str | None = None,
    ) -> list[WorkItem]:
        return []

    async def get(self, item_id: str) -> WorkItem | None:
        return None

    async def checkout(
        self,
        item_id: str,
        actor_id: str,
        *,
        lease_duration_seconds: int = 300,
    ) -> WorkItem:
        raise NotImplementedError("NullWorkItemStore does not support checkout")

    async def renew_lease(
        self,
        item_id: str,
        actor_id: str,
        *,
        lease_duration_seconds: int = 300,
    ) -> WorkItem:
        raise NotImplementedError("NullWorkItemStore does not support renew_lease")

    async def update_progress(
        self,
        item_id: str,
        *,
        progress: dict[str, Any],
    ) -> WorkItem:
        raise NotImplementedError("NullWorkItemStore does not support update_progress")

    async def complete(
        self,
        item_id: str,
        *,
        result: dict[str, Any] | None = None,
        handoff_note: HandoffNote | None = None,
        review_required: bool = False,
    ) -> WorkItem:
        raise NotImplementedError("NullWorkItemStore does not support complete")

    async def release(
        self,
        item_id: str,
        *,
        handoff_note: HandoffNote | None = None,
    ) -> WorkItem:
        raise NotImplementedError("NullWorkItemStore does not support release")

    async def fail(
        self,
        item_id: str,
        *,
        error: str,
        handoff_note: HandoffNote | None = None,
        retry: bool = False,
    ) -> WorkItem:
        raise NotImplementedError("NullWorkItemStore does not support fail")

    async def accept(self, item_id: str) -> WorkItem:
        raise NotImplementedError("NullWorkItemStore does not support accept")

    async def reject(self, item_id: str, *, reason: str) -> WorkItem:
        raise NotImplementedError("NullWorkItemStore does not support reject")

    async def stats(self) -> dict[str, int]:
        return {}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_work_item_store(
    backend: str | None,
    *,
    sqlite_path: str = "./agent.db",
    sqlite_connection: Any = None,
    database_url: str = "",
) -> WorkItemStore:
    """Create a work-item store from config values.

    Supported backends:
      - ``sqlite``   — :class:`SqliteWorkItemStore` (single-replica, dev / edge)
      - ``postgres`` — :class:`PostgresWorkItemStore` (enterprise)
      - ``http``     — platform-routed (not yet implemented)
      - ``None``     — :class:`NullWorkItemStore` (no-op default)
    """
    if backend == "sqlite":
        from .work_item_stores.sqlite import SqliteWorkItemStore
        return SqliteWorkItemStore(
            db_path=sqlite_path,
            connection=sqlite_connection,
        )
    if backend == "postgres":
        from .work_item_stores.postgres import PostgresWorkItemStore
        if not database_url:
            raise ValueError(
                "PostgresWorkItemStore requires database_url; "
                "set server.storage.database_url in agent.yaml"
            )
        return PostgresWorkItemStore(database_url)
    if backend == "http":
        raise NotImplementedError(
            "WorkItemStore backend 'http' is not yet implemented; "
            "use 'sqlite' or leave unset (Null)."
        )
    return NullWorkItemStore()
