"""Recovery orchestrator for reducer-based state reconstruction from traces."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from fipsagents.baseagent.events import (
    ContentDelta,
    ReasoningDelta,
    StreamEvent,
    ToolCallDelta,
    ToolResultEvent,
)
from fipsagents.baseagent.state import state_schema_key
from fipsagents.server.tracing import Trace

logger = logging.getLogger(__name__)


async def recover_state(
    agent: Any,
    session_id: str,
    session_store: Any,
    trace_store: Any,
) -> Any | None:
    """Recover agent state from checkpoint + replay events from subsequent traces."""
    # Get persisted state
    state_dict = await session_store.get_state(session_id)
    checkpoint_json = state_dict.get("checkpoint_state")

    if not checkpoint_json:
        logger.debug("No checkpoint for session %s, starting fresh", session_id)
        return None

    try:
        checkpoint_data = json.loads(checkpoint_json)
    except json.JSONDecodeError:
        logger.warning("Invalid checkpoint JSON for session %s", session_id)
        return None

    expected_schema = state_schema_key(agent.state_type)
    actual_schema = checkpoint_data.get("schema_version")
    if actual_schema != expected_schema:
        logger.warning(
            "Schema mismatch for session %s: checkpoint=%s, current=%s — discarding",
            session_id, actual_schema, expected_schema,
        )
        return None

    try:
        state = agent.state_type.model_validate(checkpoint_data["state"])
    except ValidationError:
        logger.warning("Failed to deserialize checkpoint for session %s", session_id)
        return None

    last_trace_id = checkpoint_data.get("last_trace_id")
    traces = await trace_store.list_traces_for_session(
        session_id=session_id,
        after_trace_id=last_trace_id,
    )

    event_count = 0
    for trace in traces:
        events = reconstruct_events(trace)
        for event in events:
            state = agent.reduce(state, event)
            event_count += 1

    if event_count > 0:
        logger.info("Recovered state for session %s: replayed %d events", session_id, event_count)
    else:
        logger.debug("Recovered state for session %s from checkpoint (no replay)", session_id)

    return state


def reconstruct_events(trace: Trace) -> list[StreamEvent]:
    """Extract StreamEvent objects from a Trace's span events."""
    events: list[tuple[float, StreamEvent]] = []

    # Walk all spans in the trace
    for span in trace.spans:
        for event_dict in span.events:
            name = event_dict.get("name")
            timestamp = event_dict.get("timestamp", 0.0)
            body = event_dict.get("body", {})

            stream_event: StreamEvent | None = None

            if name == "tool_result":
                stream_event = ToolResultEvent(
                    call_id=body["call_id"],
                    name=body["name"],
                    content=body["content"],
                    is_error=body.get("is_error", False),
                )
            elif name == "content_delta":
                stream_event = ContentDelta(content=body["content"])
            elif name == "reasoning_delta":
                stream_event = ReasoningDelta(content=body["content"])
            elif name == "tool_call_delta":
                stream_event = ToolCallDelta(
                    index=body["index"],
                    call_id=body.get("call_id"),
                    name=body.get("name"),
                    arguments_delta=body.get("arguments_delta", ""),
                )
            # Skip messages_snapshot and unknown events

            if stream_event is not None:
                events.append((timestamp, stream_event))

    # Sort by timestamp
    events.sort(key=lambda x: x[0])

    return [event for _, event in events]
