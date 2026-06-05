"""Factory for work-item coordination stock tools.

Call :func:`make_work_item_tools` once per agent instance during setup.
The returned list of callables is decorated with ``@tool`` and ready to pass to
``ToolRegistry.register``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fipsagents.baseagent.events import (
    StageDemoted,
    StagePromoted,
    WorkItemCheckedOut,
    WorkItemCompleted,
    WorkItemReleased,
)
from fipsagents.baseagent.tools import tool
from fipsagents.baseagent.tools._stock import StockToolSpec
from fipsagents.server.work_items import HandoffNote

logger = logging.getLogger("fipsagents.work_items_tool")


def _handoff_to_dict(note: HandoffNote) -> dict[str, Any]:
    """Serialize HandoffNote to dict."""
    return {
        "accomplished": note.accomplished,
        "attempted": note.attempted,
        "remaining": note.remaining,
        "blockers": note.blockers,
        "artifacts": note.artifacts,
        "context": note.context,
    }


def _capture_stage_before(agent: object):
    """Snapshot the maturation stage before a trust mutation.

    Returns the current ``MaturationStage`` or ``None`` when maturation is
    not active.  Call this *before* ``TrustManager.record_*`` so the
    pre-mutation stage is available for comparison.
    """
    mm = getattr(agent, "_maturation_manager", None)
    return mm.current_stage() if mm is not None else None


def _drain_trust_events(agent: object, stage_before=None) -> None:
    """Drain pending trust events, emit maturation stage transitions, and quarantine.

    Called after ``TrustManager.record_completion`` or ``record_failure`` to
    flush ``TrustLevelChanged`` events into ``_self_healing_events`` and, when
    a maturation manager is active, emit ``StagePromoted`` / ``StageDemoted``
    events when the trust-level change crosses a stage boundary.

    *stage_before* should be the return value of ``_capture_stage_before()``
    taken before the trust mutation.
    """
    trust = getattr(agent, "_trust_manager", None)
    if trust is None:
        return

    trust_events = trust.drain_events()
    if not trust_events:
        return

    sh_buf = getattr(agent, "_self_healing_events", None)
    if sh_buf is not None:
        sh_buf.extend(trust_events)

    # Check for maturation stage transitions.
    mm = getattr(agent, "_maturation_manager", None)
    if mm is not None and stage_before is not None:
        stage_after = mm.current_stage()
        if stage_after != stage_before:
            # Determine direction from the trust level change.
            last_ev = trust_events[-1]
            if last_ev.to_level > last_ev.from_level:
                event = StagePromoted(
                    from_stage=stage_before.value,
                    to_stage=stage_after.value,
                    trust_level=last_ev.to_level,
                    reason=last_ev.reason,
                )
            else:
                event = StageDemoted(
                    from_stage=stage_before.value,
                    to_stage=stage_after.value,
                    trust_level=last_ev.to_level,
                    reason=last_ev.reason,
                )
            if sh_buf is not None:
                sh_buf.append(event)

    # On demotion, quarantine out-of-scope learned skills.
    for ev in trust_events:
        if hasattr(ev, "from_level") and ev.to_level < ev.from_level:
            cfg = getattr(getattr(agent, "config", None), "self_healing", None)
            if cfg is not None and cfg.enabled:
                from fipsagents.baseagent.maturation import quarantine_out_of_scope_skills

                learned_dir = Path(cfg.learned_skills_dir)
                if not learned_dir.is_absolute():
                    learned_dir = getattr(agent, "_base_dir", Path(".")) / learned_dir
                q_events = quarantine_out_of_scope_skills(
                    learned_dir, ev.to_level, cfg.trust_domains,
                )
                if q_events and sh_buf is not None:
                    sh_buf.extend(q_events)


def make_work_item_tools(agent: object) -> list:
    """Build the work-item coordination tools for this agent.

    Returns:
        List of 5 ``@tool``-decorated async functions ready for
        ``ToolRegistry.register``.
    """

    def _get_store():
        """Retrieve the work-item store from agent, raising if not configured."""
        store = getattr(agent, "_work_item_store", None)
        if store is None:
            raise RuntimeError("Work item store not configured")
        return store

    def _get_actor_id() -> str:
        """Retrieve the actor ID from agent attributes."""
        return getattr(agent, "_work_item_actor_id", None) or "unknown"

    def _emit(event):
        """Append *event* to ``agent._work_item_events`` defensively."""
        buf = getattr(agent, "_work_item_events", None)
        if buf is not None:
            buf.append(event)

    @tool(
        description=(
            "List available work items from the pool that match your capabilities. "
            "Returns items ordered by priority (highest first)."
        ),
        visibility="llm_only",
        name="check_available_work",
    )
    async def check_available_work(max_results: int = 5) -> str:
        """List work items available for checkout.

        Args:
            max_results: Maximum number of items to return.

        Returns:
            JSON array of work items with id, title, description, priority,
            and handoff_note.
        """
        store = _get_store()
        caps = getattr(agent, "_discovered_capabilities", None) or None
        items = await store.list_available(capabilities=caps, max_results=max_results)
        return json.dumps(
            [
                {
                    "id": item.id,
                    "title": item.title,
                    "description": item.description,
                    "priority": item.priority,
                    "handoff_note": (
                        _handoff_to_dict(item.handoff_note)
                        if item.handoff_note
                        else None
                    ),
                }
                for item in items
            ]
        )

    @tool(
        description=(
            "Check out a work item from the pool and claim it for processing. "
            "Only one agent can hold a work item at a time. "
            "The lease auto-expires if not renewed."
        ),
        visibility="llm_only",
        name="checkout_work_item",
    )
    async def checkout_work_item(
        item_id: str, lease_duration_seconds: int = 300
    ) -> str:
        """Check out and claim a work item.

        Args:
            item_id: ID of the work item to check out.
            lease_duration_seconds: How long to hold the lease before auto-expire.

        Returns:
            JSON object with full work item details including acceptance_criteria
            and handoff_note.
        """
        store = _get_store()
        actor = _get_actor_id()
        item = await store.checkout(
            item_id, actor, lease_duration_seconds=lease_duration_seconds
        )
        agent._checked_out_work_item = item
        _emit(WorkItemCheckedOut(item_id=item.id, actor_id=actor, title=item.title))
        result = {
            "id": item.id,
            "title": item.title,
            "description": item.description,
            "acceptance_criteria": item.acceptance_criteria,
            "handoff_note": (
                _handoff_to_dict(item.handoff_note) if item.handoff_note else None
            ),
            "lease_expires_at": item.lease_expires_at,
        }
        return json.dumps(result)

    @tool(
        description=(
            "Mark a checked-out work item as complete. "
            "Provide a summary of what was accomplished."
        ),
        visibility="llm_only",
        name="complete_work_item",
    )
    async def complete_work_item(
        item_id: str,
        result_summary: str,
        accomplished: list[str],
        review_required: bool = False,
    ) -> str:
        """Complete a work item.

        Args:
            item_id: ID of the work item to complete.
            result_summary: Summary of what was accomplished.
            accomplished: List of specific accomplishments.
            review_required: Whether human review is needed before final acceptance.

        Returns:
            JSON object with item id, status, and title.
        """
        store = _get_store()
        actor = _get_actor_id()
        handoff = HandoffNote(accomplished=accomplished)
        item = await store.complete(
            item_id,
            result={"summary": result_summary},
            handoff_note=handoff,
            review_required=review_required,
        )
        agent._checked_out_work_item = None
        _emit(WorkItemCompleted(item_id=item.id, actor_id=actor, title=item.title))

        # Record successful completion in the trust manager.
        trust = getattr(agent, "_trust_manager", None)
        if trust is not None:
            stage_before = _capture_stage_before(agent)
            trust.record_completion(reason=f"completed work item {item_id}")
            _drain_trust_events(agent, stage_before=stage_before)

        return json.dumps(
            {"id": item.id, "status": item.status.value, "title": item.title}
        )

    @tool(
        description=(
            "Release a work item back to the pool with a structured handoff note "
            "for the next agent."
        ),
        visibility="llm_only",
        name="release_work_item",
    )
    async def release_work_item(
        item_id: str,
        accomplished: list[str],
        remaining: list[str],
        blockers: list[str] | None = None,
        context: str = "",
    ) -> str:
        """Release a work item back to the pool.

        Args:
            item_id: ID of the work item to release.
            accomplished: What was completed during this checkout.
            remaining: What still needs to be done.
            blockers: Issues preventing further progress.
            context: Additional context for the next agent.

        Returns:
            JSON object with item id, status, and title.
        """
        store = _get_store()
        actor = _get_actor_id()
        handoff = HandoffNote(
            accomplished=accomplished,
            remaining=remaining,
            blockers=blockers or [],
            context=context,
        )
        item = await store.release(item_id, handoff_note=handoff)
        agent._checked_out_work_item = None
        _emit(WorkItemReleased(item_id=item.id, actor_id=actor, title=item.title))

        # Record release as a trust failure (agent couldn't finish the item).
        reason = "; ".join(remaining[:3]) if remaining else "no details"
        trust = getattr(agent, "_trust_manager", None)
        if trust is not None:
            stage_before = _capture_stage_before(agent)
            trust.record_failure(
                reason=f"released work item {item_id}: {reason}",
            )
            _drain_trust_events(agent, stage_before=stage_before)

        return json.dumps(
            {"id": item.id, "status": item.status.value, "title": item.title}
        )

    @tool(
        description=(
            "Update progress on a checked-out work item. "
            "Implicitly renews the lease."
        ),
        visibility="llm_only",
        name="update_work_progress",
    )
    async def update_work_progress(
        item_id: str,
        status_message: str,
        accomplished_so_far: list[str] | None = None,
    ) -> str:
        """Update progress on a work item.

        Args:
            item_id: ID of the work item.
            status_message: Current status message.
            accomplished_so_far: Optional list of what has been done so far.

        Returns:
            JSON object with item id, status, and updated_at timestamp.
        """
        store = _get_store()
        progress = {"status_message": status_message}
        if accomplished_so_far:
            progress["accomplished_so_far"] = accomplished_so_far
        item = await store.update_progress(item_id, progress=progress)
        return json.dumps(
            {
                "id": item.id,
                "status": item.status.value,
                "updated_at": item.updated_at,
            }
        )

    return [
        check_available_work,
        checkout_work_item,
        complete_work_item,
        release_work_item,
        update_work_progress,
    ]


STOCK_TOOL_SPEC = StockToolSpec(
    factory=make_work_item_tools,
    condition=lambda agent: (
        hasattr(agent, "config")
        and hasattr(getattr(agent, "config", None), "server")
        and getattr(
            getattr(getattr(agent, "config", None), "server", None),
            "work_items",
            None,
        )
        is not None
        and getattr(
            getattr(getattr(agent, "config", None), "server", None),
            "work_items",
            None,
        ).enabled
    ),
)
