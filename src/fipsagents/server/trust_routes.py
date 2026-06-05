"""REST API route handlers for agent trust scoreboard.

Provides read-only observability endpoints for agent trust state, skills,
and discovered capabilities. These endpoints are intended for dashboards,
monitoring tools, and operator visibility.

Separated from ``app.py`` to keep the main server module manageable.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _get_trust(request: Request) -> JSONResponse:
    """GET /v1/agent/trust -- get current trust state."""
    get_agent = request.app.state.get_agent
    agent = get_agent()

    if agent is None:
        return JSONResponse(
            {"error": "Agent not available"}, status_code=404,
        )

    trust = getattr(agent, "_trust_manager", None)
    if trust is None:
        return JSONResponse(
            {"error": "Trust not enabled"}, status_code=404,
        )

    state = trust.get_state()
    return JSONResponse({
        "level": state.level,
        "score": state.score,
        "completions": state.completions,
        "failures": state.failures,
        "violations": state.violations,
        "last_promotion": state.last_promotion,
        "last_decay": state.last_decay,
        "history": [
            {
                "timestamp": e.timestamp,
                "event_type": e.event_type,
                "delta": e.delta,
                "reason": e.reason,
                "resulting_level": e.resulting_level,
                "resulting_score": e.resulting_score,
            }
            for e in state.history
        ],
    })


async def _get_skills(request: Request) -> JSONResponse:
    """GET /v1/agent/skills -- list all skills (bundled and learned)."""
    get_agent = request.app.state.get_agent
    agent = get_agent()

    if agent is None:
        return JSONResponse(
            {"error": "Agent not available"}, status_code=404,
        )

    skill_loader = getattr(agent, "skills", None)
    if skill_loader is None:
        return JSONResponse(
            {"error": "Skills not enabled"}, status_code=404,
        )

    # Access the internal skills dict from SkillLoader
    skills_dict = getattr(skill_loader, "_skills", {})

    skills = []
    for skill in skills_dict.values():
        skills.append({
            "name": skill.name,
            "description": skill.description,
            "learned": skill.learned,
            "activated": skill.activated,
        })

    return JSONResponse({"skills": skills})


async def _get_capabilities(request: Request) -> JSONResponse:
    """GET /v1/agent/capabilities -- get discovered capabilities."""
    get_agent = request.app.state.get_agent
    agent = get_agent()

    if agent is None:
        return JSONResponse(
            {"error": "Agent not available"}, status_code=404,
        )

    capabilities_dict = getattr(agent, "_discovered_capabilities", {})

    capabilities = [
        {"name": name, "value": value}
        for name, value in capabilities_dict.items()
    ]

    return JSONResponse({"capabilities": capabilities})


async def _get_maturation(request: Request) -> JSONResponse:
    """GET /v1/agent/maturation -- get maturation stage summary."""
    get_agent = request.app.state.get_agent
    agent = get_agent()

    if agent is None:
        return JSONResponse(
            {"error": "Agent not available"}, status_code=404,
        )

    maturation = getattr(agent, "_maturation_manager", None)
    if maturation is None:
        return JSONResponse(
            {"error": "Maturation not enabled"}, status_code=404,
        )

    return JSONResponse(maturation.get_summary())


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_trust_routes(
    app: Any,
    get_agent: Callable,
) -> None:
    """Register trust/scoreboard routes on the ASGI app.

    Routes are registered unconditionally. Each handler checks whether
    the agent and its components are available at request time (they may
    be ``None`` before the agent is initialized or when features are
    disabled).

    Args:
        app: The FastAPI/Starlette application instance.
        get_agent: A callable returning the current agent instance
            (or ``None``).
    """
    # Stash the accessor on app.state so handlers can retrieve it from
    # the request without closure capture.
    app.state.get_agent = get_agent

    app.add_route("/v1/agent/trust", _get_trust, methods=["GET"])
    app.add_route("/v1/agent/skills", _get_skills, methods=["GET"])
    app.add_route("/v1/agent/capabilities", _get_capabilities, methods=["GET"])
    app.add_route("/v1/agent/maturation", _get_maturation, methods=["GET"])
