"""REST API route handlers for work-item management.

Provides CRUD endpoints for external work-item management (dashboards,
CI/CD pipelines, monitoring). Separated from ``app.py`` to keep the main
server module manageable.

Endpoints use Starlette request/response directly (same as the rest of
app.py under the hood) and are registered via ``register_work_item_routes``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .work_items import (
    Capability,
    HandoffNote,
    NullWorkItemStore,
    WorkItem,
    WorkItemStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_item(item: WorkItem) -> dict[str, Any]:
    """Serialize a WorkItem to a JSON-safe dict."""
    d: dict[str, Any] = {
        "id": item.id,
        "title": item.title,
        "description": item.description,
        "status": item.status.value if isinstance(item.status, WorkItemStatus) else item.status,
        "priority": item.priority,
        "assignee": item.assignee,
        "parent_id": item.parent_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
    if item.required_capabilities:
        d["required_capabilities"] = [
            {"name": c.name, "value": c.value}
            for c in item.required_capabilities
        ]
    if item.max_tokens is not None:
        d["max_tokens"] = item.max_tokens
    if item.max_cost_usd is not None:
        d["max_cost_usd"] = item.max_cost_usd
    if item.max_duration_seconds is not None:
        d["max_duration_seconds"] = item.max_duration_seconds
    if item.depends_on:
        d["depends_on"] = item.depends_on
    if item.acceptance_criteria:
        d["acceptance_criteria"] = item.acceptance_criteria
    if item.created_by:
        d["created_by"] = item.created_by
    if item.handoff_note:
        d["handoff_note"] = {
            "accomplished": item.handoff_note.accomplished,
            "attempted": item.handoff_note.attempted,
            "remaining": item.handoff_note.remaining,
            "blockers": item.handoff_note.blockers,
            "artifacts": item.handoff_note.artifacts,
            "context": item.handoff_note.context,
        }
    # progress is stored on the DB but not a formal dataclass field
    progress = item.__dict__.get("progress")
    if progress:
        d["progress"] = progress
    return d


def _store_guard(get_store: Callable) -> Any | None:
    """Return the store if usable, or None."""
    store = get_store()
    if store is None or isinstance(store, NullWorkItemStore):
        return None
    return store


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _stats_work_items(request: Request) -> Response:
    """GET /v1/work-items/stats -- aggregate counts by status."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse(
            {"error": "Work items not enabled"}, status_code=404,
        )
    counts = await store.stats()
    return JSONResponse({"counts": counts, "total": sum(counts.values())})


async def _create_work_item(request: Request) -> Response:
    """POST /v1/work-items -- create a new work item."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse({"error": "Work items not enabled"}, status_code=404)

    body = await request.json()
    title = body.get("title")
    if not title:
        return JSONResponse(
            {"error": "title is required"}, status_code=400,
        )

    caps = [
        Capability(name=c["name"], value=c.get("value", 1.0))
        for c in (body.get("required_capabilities") or [])
    ]

    item = WorkItem(
        id=f"wi_{uuid.uuid4().hex[:16]}",
        title=title,
        description=body.get("description", ""),
        priority=body.get("priority", 0),
        required_capabilities=caps,
        max_tokens=body.get("max_tokens"),
        max_cost_usd=body.get("max_cost_usd"),
        max_duration_seconds=body.get("max_duration_seconds"),
        parent_id=body.get("parent_id"),
        depends_on=body.get("depends_on", []),
        acceptance_criteria=body.get("acceptance_criteria", []),
        created_by=body.get("created_by", ""),
    )

    created = await store.create(item)
    return JSONResponse(_serialize_item(created), status_code=201)


async def _list_work_items(request: Request) -> Response:
    """GET /v1/work-items -- list work items."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse({"error": "Work items not enabled"}, status_code=404)

    max_results = int(request.query_params.get("max_results", "20"))
    parent_id = request.query_params.get("parent_id")

    items = await store.list_available(
        max_results=max_results, parent_id=parent_id,
    )
    return JSONResponse([_serialize_item(i) for i in items])


async def _get_work_item(request: Request) -> Response:
    """GET /v1/work-items/{item_id} -- get a single work item."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse({"error": "Work items not enabled"}, status_code=404)

    item_id = request.path_params["item_id"]
    item = await store.get(item_id)
    if item is None:
        return JSONResponse(
            {"error": f"Work item {item_id!r} not found"}, status_code=404,
        )
    return JSONResponse(_serialize_item(item))


async def _checkout_work_item(request: Request) -> Response:
    """POST /v1/work-items/{item_id}/checkout -- check out a work item."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse({"error": "Work items not enabled"}, status_code=404)

    item_id = request.path_params["item_id"]
    body = await request.json()

    actor_id = body.get("actor_id")
    if not actor_id:
        return JSONResponse(
            {"error": "actor_id is required"}, status_code=400,
        )

    lease_duration = body.get("lease_duration_seconds", 300)

    try:
        item = await store.checkout(
            item_id, actor_id, lease_duration_seconds=lease_duration,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    return JSONResponse(_serialize_item(item))


async def _complete_work_item(request: Request) -> Response:
    """POST /v1/work-items/{item_id}/complete -- complete a work item."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse({"error": "Work items not enabled"}, status_code=404)

    item_id = request.path_params["item_id"]
    body = await request.json()

    result = body.get("result")
    review_required = body.get("review_required", False)

    note: HandoffNote | None = None
    if body.get("accomplished"):
        note = HandoffNote(
            accomplished=body.get("accomplished", []),
            attempted=body.get("attempted", []),
            remaining=body.get("remaining", []),
            blockers=body.get("blockers", []),
            artifacts=body.get("artifacts", {}),
            context=body.get("context", ""),
        )

    try:
        item = await store.complete(
            item_id,
            result=result,
            handoff_note=note,
            review_required=review_required,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    return JSONResponse(_serialize_item(item))


async def _release_work_item(request: Request) -> Response:
    """POST /v1/work-items/{item_id}/release -- release a work item."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse({"error": "Work items not enabled"}, status_code=404)

    item_id = request.path_params["item_id"]
    body = await request.json()

    note = HandoffNote(
        accomplished=body.get("accomplished", []),
        remaining=body.get("remaining", []),
        blockers=body.get("blockers", []),
        context=body.get("context", ""),
    )

    try:
        item = await store.release(item_id, handoff_note=note)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    return JSONResponse(_serialize_item(item))


async def _accept_work_item(request: Request) -> Response:
    """POST /v1/work-items/{item_id}/accept -- accept a reviewed work item."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse({"error": "Work items not enabled"}, status_code=404)

    item_id = request.path_params["item_id"]

    try:
        item = await store.accept(item_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    return JSONResponse(_serialize_item(item))


async def _reject_work_item(request: Request) -> Response:
    """POST /v1/work-items/{item_id}/reject -- reject a reviewed work item."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse({"error": "Work items not enabled"}, status_code=404)

    item_id = request.path_params["item_id"]
    body = await request.json()

    reason = body.get("reason")
    if not reason:
        return JSONResponse(
            {"error": "reason is required"}, status_code=400,
        )

    try:
        item = await store.reject(item_id, reason=reason)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    return JSONResponse(_serialize_item(item))


async def _delete_work_item(request: Request) -> Response:
    """DELETE /v1/work-items/{item_id} -- cancel a work item."""
    get_store = request.app.state.get_work_item_store
    store = _store_guard(get_store)
    if store is None:
        return JSONResponse({"error": "Work items not enabled"}, status_code=404)

    item_id = request.path_params["item_id"]

    try:
        await store.fail(item_id, error="cancelled_via_api")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_work_item_routes(
    app: Any,
    get_store: Callable,
) -> None:
    """Register work-item CRUD routes on the ASGI app.

    Routes are registered unconditionally. Each handler checks whether
    the store is available at request time (it may be ``None`` before
    lifespan completes or when work items are disabled).

    Args:
        app: The FastAPI/Starlette application instance.
        get_store: A callable returning the current ``WorkItemStore``
            (or ``None``).
    """
    # Stash the accessor on app.state so handlers can retrieve it from
    # the request without closure capture.
    app.state.get_work_item_store = get_store

    app.add_route("/v1/work-items", _create_work_item, methods=["POST"])
    app.add_route("/v1/work-items", _list_work_items, methods=["GET"])
    app.add_route(
        "/v1/work-items/stats", _stats_work_items, methods=["GET"],
    )
    app.add_route(
        "/v1/work-items/{item_id}", _get_work_item, methods=["GET"],
    )
    app.add_route(
        "/v1/work-items/{item_id}/checkout",
        _checkout_work_item,
        methods=["POST"],
    )
    app.add_route(
        "/v1/work-items/{item_id}/complete",
        _complete_work_item,
        methods=["POST"],
    )
    app.add_route(
        "/v1/work-items/{item_id}/release",
        _release_work_item,
        methods=["POST"],
    )
    app.add_route(
        "/v1/work-items/{item_id}/accept",
        _accept_work_item,
        methods=["POST"],
    )
    app.add_route(
        "/v1/work-items/{item_id}/reject",
        _reject_work_item,
        methods=["POST"],
    )
    app.add_route(
        "/v1/work-items/{item_id}", _delete_work_item, methods=["DELETE"],
    )
