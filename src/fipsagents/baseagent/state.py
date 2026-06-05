"""Agent state management for reducer-based state recovery.

AgentState is the typed base for agent-defined state. StateCheckpoint
captures a snapshot for persistence. StateReducerObserver wraps the
event stream to drive ``reduce()`` and ``after_event()`` hooks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, AsyncIterator

from pydantic import BaseModel, ConfigDict

from .events import StreamEvent


class AgentState(BaseModel):
    """Base class for reducer-managed agent state.

    Parallel to ``WorkflowState`` but for continuous agent loops.
    """

    model_config = ConfigDict(extra="forbid")


@dataclass
class StateCheckpoint:
    """Snapshot of agent state at a point in the event log."""

    state: dict[str, Any]
    last_trace_id: str
    last_span_id: str
    checkpoint_at: str
    schema_version: str


def state_schema_key(cls: type) -> str:
    """Deterministic fingerprint from a Pydantic model's field schema."""
    if not hasattr(cls, "model_fields"):
        return cls.__qualname__
    parts = []
    for name, field_info in sorted(cls.model_fields.items()):
        parts.append(f"{name}:{field_info.annotation}")
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return f"{cls.__qualname__}:{digest}"


class StateReducerObserver:
    """Wraps an event stream, calling ``reduce()`` and ``after_event()``
    on each event.  Follows the TraceCollector/MetricsCollector observer
    pattern — events pass through unchanged.
    """

    def __init__(self, agent: Any, *, replay: bool = False) -> None:
        self._agent = agent
        self._replay = replay

    async def observe(
        self, events: AsyncIterator[StreamEvent],
    ) -> AsyncIterator[StreamEvent]:
        async for event in events:
            if self._agent._agent_state is not None:
                self._agent._agent_state = self._agent.reduce(
                    self._agent._agent_state, event,
                )
                if not self._replay:
                    await self._agent.after_event(
                        self._agent._agent_state, event,
                    )
            yield event
