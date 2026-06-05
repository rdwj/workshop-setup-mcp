"""Factory for the ``delegate_to_agent`` stock tool.

Call :func:`make_delegate_tool` once per agent instance during setup.
The returned callable is decorated with ``@tool`` and ready to pass to
``ToolRegistry.register``.

Transport injection: pass ``transport_factory`` to inject a fake transport
in tests without monkeypatching.  Signature:
``(name: str, config: SubagentConfig) -> SubagentTransport``.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Callable

from fipsagents.baseagent.events import SubagentCompleted, SubagentFailed, SubagentInvoked
from fipsagents.baseagent.tools import tool
from fipsagents.baseagent.tools._stock import StockToolSpec
from fipsagents.subagents.transport import (
    InProcessSubagentTransport,
    RemoteSubagentTransport,
    SubagentTransport,
)
from fipsagents.subagents.types import (
    MaxDelegationDepthError,
    SubagentError,
    SubagentResult,
)

try:
    from fipsagents.server.propagation import inject_trace_context
except ImportError:  # server extras not installed — synthesise nothing
    inject_trace_context = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from fipsagents.baseagent.config import SubagentConfig

logger = logging.getLogger("fipsagents.subagent_tool")

# Per-(agent_id, agent_name) set to deduplicate permission_scope warnings.
# Uses id(agent) as key so different agent instances are independent.
_warned_permission_scope: set[tuple[int, str]] = set()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_subagent_menu(agent: object) -> str:
    """Build the bullet list of subagents for the tool description.

    Returns an empty string when ``agent.subagents`` is empty (the tool
    will not be registered in that case, per Step 6's contract).
    """
    subagents: dict = getattr(agent, "subagents", {})
    if not subagents:
        return ""
    lines: list[str] = []
    for name, cfg in subagents.items():
        lines.append(f"- {name}: {cfg.when_to_use}")
    return "\n".join(lines)


def _emit(agent: object, event: object) -> None:
    """Append *event* to ``agent._subagent_events`` defensively.

    If the attribute is absent (e.g. in unit tests that stub only part of
    the contract), the emit is a no-op rather than a crash.
    """
    buf = getattr(agent, "_subagent_events", None)
    if buf is not None:
        buf.append(event)


def _build_transport(name: str, config: "SubagentConfig") -> SubagentTransport:
    """Construct the appropriate transport for *config*.

    Raises ``ValueError`` for unknown transport types — this is a config bug
    that surfaces at tool-call time, not at startup.
    """
    transport_cfg = config.transport
    if transport_cfg.type == "remote":
        return RemoteSubagentTransport(name, transport_cfg)
    if transport_cfg.type == "inprocess":
        return InProcessSubagentTransport(name, transport_cfg)
    raise ValueError(
        f"Unknown transport type: {transport_cfg.type!r} for subagent {name!r}. "
        "Expected 'remote' or 'inprocess'."
    )


# ---------------------------------------------------------------------------
# Core delegation logic
# ---------------------------------------------------------------------------


async def _delegate(
    agent: object,
    agent_name: str,
    task: str,
    context: str,
    transport_factory: Callable[[str, "SubagentConfig"], SubagentTransport],
) -> str:
    """Run one subagent delegation; return JSON on success, raise on failure."""
    from fipsagents.baseagent.config import IdentityServiceAccount

    # 1. Resolve config.
    subagents: dict = getattr(agent, "subagents", {})
    if agent_name not in subagents:
        available = list(subagents.keys())
        raise ValueError(
            f"Unknown subagent {agent_name!r}. "
            f"Available: {available}"
        )
    config: SubagentConfig = subagents[agent_name]

    # 2. Permission scope warning (logged once per (agent, agent_name)).
    if config.permission_scope is not None:
        key = (id(agent), agent_name)
        if key not in _warned_permission_scope:
            _warned_permission_scope.add(key)
            logger.warning(
                "subagent_tool: permission_scope %r set on %r but enforcement "
                "is not implemented in v1 (#164 follow-up)",
                config.permission_scope,
                agent_name,
            )

    # 3. Depth check.
    current_depth: int = getattr(agent, "_delegation_depth", 0)
    span_id = f"subagent-{uuid.uuid4().hex[:12]}"
    if current_depth + 1 > config.max_depth:
        failed_event = SubagentFailed(
            agent_name=agent_name,
            span_id=span_id,
            error_type="MaxDelegationDepthError",
            error_message=(
                f"depth {current_depth + 1} exceeds max_depth {config.max_depth}"
            ),
        )
        _emit(agent, failed_event)
        raise MaxDelegationDepthError(
            agent_name,
            depth=current_depth + 1,
            max_depth=config.max_depth,
        )

    # 4. Build transport.
    transport = transport_factory(agent_name, config)

    # 5. Build outgoing headers.
    headers: dict[str, str] = {
        "x-subagent-depth": str(current_depth + 1),
    }
    # W3C Trace Context: synthesise a traceparent from span_id so the
    # receiving subagent always has a valid traceparent to log/forward.
    # v2 will use the parent's active OTEL span context instead.
    if inject_trace_context is not None:
        headers.update(inject_trace_context(trace_id=span_id, span_id=span_id))
    if config.identity == "inherit":
        inbound = getattr(agent, "_inbound_auth_header", None)
        if inbound:
            headers["authorization"] = inbound
    elif isinstance(config.identity, IdentityServiceAccount):
        # v1: service-account injection not yet implemented.
        logger.debug(
            "subagent_tool: identity service_account=%r on %r — "
            "v1 does not inject service-account headers; "
            "see #164 for follow-up implementation.",
            config.identity.service_account,
            agent_name,
        )

    # 6. Emit SubagentInvoked.
    transport_label = config.transport.type
    _emit(
        agent,
        SubagentInvoked(
            agent_name=agent_name,
            task=task,
            span_id=span_id,
            transport=transport_label,
            depth=current_depth + 1,
        ),
    )

    # 7. Determine timeout.
    timeout = getattr(config.transport, "timeout_seconds", 60.0)

    # 8. Invoke transport.
    try:
        result: SubagentResult = await transport.invoke(
            task=task,
            context=context,
            headers=headers,
            timeout_seconds=timeout,
        )
    except SubagentError as exc:
        _emit(
            agent,
            SubagentFailed(
                agent_name=agent_name,
                span_id=span_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            ),
        )
        raise
    except Exception as exc:
        _emit(
            agent,
            SubagentFailed(
                agent_name=agent_name,
                span_id=span_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            ),
        )
        raise

    # 9. On success: stamp span_id, emit completed, record tokens, return JSON.
    result.span_id = span_id

    _emit(
        agent,
        SubagentCompleted(
            agent_name=agent_name,
            span_id=span_id,
            content=result.content,
            tokens_used=result.tokens_used,
            tool_calls_made=result.tool_calls_made,
            cost_usd=result.cost_usd,
        ),
    )

    token_buf = getattr(agent, "_subagent_token_usage", None)
    if token_buf is not None:
        token_buf.append(result.tokens_used)

    return json.dumps(asdict(result))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def make_delegate_tool(
    agent: object,
    *,
    transport_factory: Callable[[str, "SubagentConfig"], SubagentTransport] | None = None,
) -> Callable:
    """Build the per-agent ``delegate_to_agent`` tool function.

    The returned callable is ``@tool``-decorated and ready for
    ``ToolRegistry.register``.  The agent's ``subagents`` registry is
    captured by closure so ``agent_name`` routes to the right transport.

    ``transport_factory`` defaults to :func:`_build_transport`; override
    it in tests to inject a fake without monkeypatching.
    """
    _factory = transport_factory if transport_factory is not None else _build_transport

    menu = _format_subagent_menu(agent)
    description = (
        "Delegate part of the current turn to a registered subagent. "
        "Returns a JSON object with the subagent's result content, "
        "token usage, and tool-call count.\n\n"
        "Available subagents (selected via ``agent_name``):\n"
        + menu
    )

    @tool(description=description, visibility="both", name="delegate_to_agent")
    async def delegate_to_agent(
        agent_name: str,
        task: str,
        context: str = "",
    ) -> str:
        """Delegate a task to a registered subagent.

        Args:
            agent_name: Name of the subagent to invoke. Must be one of the
                agents listed in this tool's description.
            task: The task or question to delegate. Be specific and
                self-contained — the subagent does not see the parent's
                conversation history.
            context: Optional additional context. Prepended to the task.

        Returns:
            JSON-serialized ``SubagentResult``: ``{"agent_name": ...,
            "content": ..., "tokens_used": {...}, "tool_calls_made": ...,
            "cost_usd": ..., "finish_reason": ..., "span_id": ...}``.
        """
        return await _delegate(agent, agent_name, task, context, _factory)

    return delegate_to_agent


# ---------------------------------------------------------------------------
# Stock tool registration
# ---------------------------------------------------------------------------

STOCK_TOOL_SPEC = StockToolSpec(
    factory=make_delegate_tool,
    condition=lambda agent: bool(getattr(agent, "subagents", {})),
)
