"""Factory for the ``spawn_agent`` stock tool.

Call :func:`make_spawn_tool` once per agent instance during setup.
The returned callable is decorated with ``@tool`` and ready to pass to
``ToolRegistry.register``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import asdict

from fipsagents.baseagent.config import (
    AgentConfig,
    LLMConfig,
    SpawnConfig,
    _ADAPTER_ENDPOINT,
    _OFF_PLATFORM_PROVIDERS,
)
from fipsagents.baseagent.events import (
    ContentDelta,
    SpawnAgentCompleted,
    SpawnAgentFailed,
    SpawnAgentInvoked,
    StreamComplete,
    ToolResultEvent,
)
from fipsagents.baseagent.llm import LLMClient
from fipsagents.baseagent.memory import NullMemoryClient
from fipsagents.baseagent.reasoning import create_reasoning_parser
from fipsagents.baseagent.tools import tool
from fipsagents.baseagent.tools._registry import ToolRegistry
from fipsagents.baseagent.tools._stock import StockToolSpec
from fipsagents.subagents.types import (
    MaxDelegationDepthError,
    SubagentCrashedError,
    SubagentResult,
    SubagentTimeoutError,
)

logger = logging.getLogger("fipsagents.spawn_tool")

_ROLE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _emit(agent: object, event: object) -> None:
    """Append *event* to ``agent._subagent_events`` defensively.

    If the attribute is absent (e.g. in unit tests that stub only part of
    the contract), the emit is a no-op rather than a crash.
    """
    buf = getattr(agent, "_subagent_events", None)
    if buf is not None:
        buf.append(event)


def _build_child_registry(
    parent_registry: ToolRegistry,
    tool_names: list[str],
) -> ToolRegistry:
    """Copy the named tools from *parent_registry* into a fresh registry."""
    child = ToolRegistry()
    for name in tool_names:
        meta = parent_registry.get(name)
        if meta is None:
            available = [t.name for t in parent_registry.get_all()]
            raise ValueError(
                f"Cannot give tool {name!r} to spawned agent — "
                f"not in parent registry. Available: {available}"
            )
        child._tools[name] = meta
    return child


# ---------------------------------------------------------------------------
# Core spawn logic
# ---------------------------------------------------------------------------


async def _spawn(
    agent: object,
    role: str,
    system_prompt: str,
    task: str,
    tools: list[str] | None,
    model: str | None,
    max_iterations: int | None,
) -> str:
    """Run one ephemeral subagent; return JSON on success, raise on failure."""

    # 1. Validate role name.
    if not _ROLE_RE.match(role):
        raise ValueError(
            f"Invalid role name: {role!r} — must match {_ROLE_RE.pattern}"
        )

    # 2. Read spawn config.
    spawn_config: SpawnConfig = getattr(
        getattr(agent, "config", None), "spawn", SpawnConfig()
    )
    if not spawn_config.enabled:
        raise ValueError("spawn_agent is disabled in agent configuration")

    # 3. Allowed model check.
    if model is not None and spawn_config.allowed_models is not None:
        if model not in spawn_config.allowed_models:
            raise ValueError(
                f"Model {model!r} not in allowed_models: "
                f"{spawn_config.allowed_models}"
            )

    # 4. Depth check.
    current_depth: int = getattr(agent, "_delegation_depth", 0)
    span_id = f"spawn-{uuid.uuid4().hex[:12]}"
    if current_depth + 1 > spawn_config.max_depth:
        _emit(agent, SpawnAgentFailed(
            role=role, span_id=span_id,
            error_type="MaxDelegationDepthError",
            error_message=(
                f"depth {current_depth + 1} exceeds "
                f"max_depth {spawn_config.max_depth}"
            ),
        ))
        raise MaxDelegationDepthError(
            role, depth=current_depth + 1, max_depth=spawn_config.max_depth,
        )

    # 5. Build child tool registry.
    tool_names = tools or []
    parent_registry: ToolRegistry = getattr(agent, "tools", ToolRegistry())
    child_registry = _build_child_registry(parent_registry, tool_names)

    # 6. Emit SpawnAgentInvoked.
    _emit(agent, SpawnAgentInvoked(
        role=role, task=task, span_id=span_id,
        tools=tool_names, model=model,
        depth=current_depth + 1,
    ))

    # 7. Build LLM client for the child.
    parent_config: AgentConfig | None = getattr(agent, "config", None)
    if parent_config is None:
        raise ValueError("Parent agent has no config — cannot spawn child")

    child_model_cfg: LLMConfig = parent_config.model
    if model is not None:
        child_model_cfg = parent_config.model.model_copy(
            update={"name": model},
        )

    effective_model_cfg = child_model_cfg
    if child_model_cfg.provider in _OFF_PLATFORM_PROVIDERS:
        effective_model_cfg = child_model_cfg.model_copy(
            update={"endpoint": _ADAPTER_ENDPOINT},
        )

    child_llm = LLMClient(effective_model_cfg)

    # 8. Iteration limit and reasoning parser.
    effective_max_iter = (
        max_iterations
        if max_iterations is not None
        else spawn_config.max_iterations
    )
    if effective_max_iter < 1:
        raise ValueError(f"max_iterations must be >= 1, got {effective_max_iter}")
    reasoning_parser = create_reasoning_parser(child_model_cfg.name)

    # 9. Construct ephemeral BaseAgent via lazy import.
    from fipsagents.baseagent.agent import BaseAgent

    class _Ephemeral(BaseAgent):
        async def step(self):
            from fipsagents.baseagent.agent import StepResult
            return StepResult.done()

    child = object.__new__(_Ephemeral)
    child.config = AgentConfig(model=child_model_cfg)
    child.llm = child_llm
    child.tools = child_registry
    child.messages = [{"role": "system", "content": system_prompt}]
    child._delegation_depth = current_depth + 1
    child._setup_done = True
    child._reasoning_parser = reasoning_parser
    child.memory = NullMemoryClient()

    # Buffers expected by astep_stream event-drain loops.
    child._subagent_events = []
    child._subagent_token_usage = []
    child._question_events = []
    child._question_pending = None
    child._work_item_events = []
    child._self_healing_events = []
    child._permission_source = None
    child._permission_mode = "enforce"
    child._permission_preapproved = set()
    child._checked_out_work_item = None
    child._headroom_warned = False

    # 10. Inject user message.
    child.messages.append({"role": "user", "content": task})

    # 11. Drive astep_stream.
    timeout = float(effective_max_iter) * 30.0

    async def _consume() -> SubagentResult:
        content_parts: list[str] = []
        tool_calls_made = 0
        finish_reason = "stop"
        tokens_used: dict[str, int] = {
            "input": 0, "output": 0, "cached": 0,
        }

        async for event in child.astep_stream(
            max_iterations=effective_max_iter,
        ):
            if isinstance(event, ContentDelta):
                content_parts.append(event.content)
            elif isinstance(event, ToolResultEvent):
                tool_calls_made += 1
            elif isinstance(event, StreamComplete):
                finish_reason = event.finish_reason
                m = event.metrics
                tokens_used = {
                    "input": int(m.prompt_tokens or 0),
                    "output": int(m.completion_tokens or 0),
                    "cached": 0,
                }

        return SubagentResult(
            agent_name=role,
            content="".join(content_parts),
            tokens_used=tokens_used,
            tool_calls_made=tool_calls_made,
            cost_usd=0.0,
            span_id=None,
            finish_reason=finish_reason,
        )

    try:
        result = await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError:
        _emit(agent, SpawnAgentFailed(
            role=role, span_id=span_id,
            error_type="SubagentTimeoutError",
            error_message=(
                f"Spawned agent {role!r} timed out after {timeout}s"
            ),
        ))
        raise SubagentTimeoutError(role, timeout)
    except (MaxDelegationDepthError, SubagentTimeoutError):
        raise
    except Exception as exc:
        _emit(agent, SpawnAgentFailed(
            role=role, span_id=span_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        ))
        raise SubagentCrashedError(role, original=exc) from exc

    # 12. Success — stamp span, emit completed, record tokens.
    result.span_id = span_id
    _emit(agent, SpawnAgentCompleted(
        role=role, span_id=span_id,
        content=result.content,
        tokens_used=result.tokens_used,
        tool_calls_made=result.tool_calls_made,
        cost_usd=result.cost_usd,
    ))

    token_buf = getattr(agent, "_subagent_token_usage", None)
    if token_buf is not None:
        token_buf.append(result.tokens_used)

    return json.dumps(asdict(result))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def make_spawn_tool(agent: object):
    """Build the per-agent ``spawn_agent`` tool function.

    The returned callable is ``@tool``-decorated and ready for
    ``ToolRegistry.register``.
    """

    @tool(
        description=(
            "Spawn an ephemeral in-process subagent with a custom system "
            "prompt and optional tool subset. The subagent executes a "
            "single task in isolation and is destroyed afterward. Returns "
            "a JSON SubagentResult with the agent's response, token "
            "usage, and tool-call count."
        ),
        visibility="llm_only",
        name="spawn_agent",
    )
    async def spawn_agent(
        role: str,
        system_prompt: str,
        task: str,
        tools: list[str] | None = None,
        model: str | None = None,
        max_iterations: int | None = None,
    ) -> str:
        """Spawn an ephemeral subagent.

        Args:
            role: Short label for the spawned agent (e.g. "researcher").
            system_prompt: Full system prompt for the spawned agent.
            task: The user message / instruction to send.
            tools: Allowlist of parent tool names to give the child. None means no tools.
            model: Override model name. Defaults to parent's model.
            max_iterations: Override max loop iterations. Defaults to config value.
        """
        return await _spawn(
            agent, role, system_prompt, task, tools, model, max_iterations,
        )

    return spawn_agent


# ---------------------------------------------------------------------------
# Stock tool registration
# ---------------------------------------------------------------------------

STOCK_TOOL_SPEC = StockToolSpec(
    factory=make_spawn_tool,
)
