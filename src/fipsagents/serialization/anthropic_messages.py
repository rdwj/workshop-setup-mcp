"""Serialize a ``BaseAgent.astep_stream()`` event sequence to Anthropic
Messages API streaming format (SSE).

Wire format reference:
https://docs.anthropic.com/en/api/messages-streaming

Anthropic uses **named SSE events** with the shape::

    event: <event_type>
    data: <json>

Content is organized into explicitly opened and closed content blocks,
each with a monotonically increasing index. Block types are ``text``,
``thinking``, and ``tool_use``.

The public entry point is :func:`stream_events_as_anthropic_messages`.
It is a pure async generator -- no FastAPI, no logging, no side effects.
Callers own the transport and any logging they want to add around
iteration.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from fipsagents.baseagent.events import (
    ContentDelta,
    ReasoningDelta,
    StreamComplete,
    StreamEvent,
    StreamMetrics,
    ToolCallDelta,
    ToolResultEvent,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STOP_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _sse_frame(event_type: str, payload: dict) -> str:
    """Format a single Anthropic named-event SSE frame."""
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


def _message_start(message_id: str, model_name: str) -> str:
    """Emit the opening ``message_start`` frame."""
    return _sse_frame("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model_name,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })


def _ping() -> str:
    return _sse_frame("ping", {"type": "ping"})


def _content_block_start(index: int, content_block: dict) -> str:
    return _sse_frame("content_block_start", {
        "type": "content_block_start",
        "index": index,
        "content_block": content_block,
    })


def _content_block_delta(index: int, delta: dict) -> str:
    return _sse_frame("content_block_delta", {
        "type": "content_block_delta",
        "index": index,
        "delta": delta,
    })


def _content_block_stop(index: int) -> str:
    return _sse_frame("content_block_stop", {
        "type": "content_block_stop",
        "index": index,
    })


def _message_delta(
    stop_reason: str,
    metrics: StreamMetrics,
) -> str:
    # stream_metrics is a custom extension (not part of the Anthropic
    # spec) carrying TTFT / ITL / counters — mirrors the OpenAI
    # serializer's stream_metrics on the usage chunk.  Conforming
    # Anthropic clients ignore unknown top-level keys.
    return _sse_frame("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": stop_reason,
            "stop_sequence": None,
        },
        "usage": {
            "output_tokens": metrics.completion_tokens or 0,
        },
        "stream_metrics": {
            "time_to_first_reasoning": metrics.time_to_first_reasoning,
            "time_to_first_content": metrics.time_to_first_content,
            "total_time": metrics.total_time,
            "inter_token_latencies": metrics.inter_token_latencies,
            "prompt_tokens": metrics.prompt_tokens,
            "total_tokens": metrics.total_tokens,
            "model_calls": metrics.model_calls,
            "tool_calls": metrics.tool_calls,
        },
    })


def _message_stop() -> str:
    return _sse_frame("message_stop", {"type": "message_stop"})


def _map_stop_reason(reason: str) -> str:
    return _STOP_REASON_MAP.get(reason, reason)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def stream_events_as_anthropic_messages(
    events: AsyncIterator[StreamEvent],
    message_id: str,
    model_name: str,
) -> AsyncIterator[str]:
    """Translate a ``StreamEvent`` sequence into Anthropic Messages SSE frames.

    Args:
        events: Async iterator of ``StreamEvent`` instances, typically from
            ``BaseAgent.astep_stream()``.
        message_id: Message identifier echoed in the ``message_start`` frame.
        model_name: Model identifier echoed in the ``message_start`` frame.

    Yields:
        SSE-encoded strings (``event: <type>\\ndata: <json>\\n\\n``).
        On exception from the source iterator an error text block is emitted
        before ``message_stop``.
    """
    yield _message_start(message_id, model_name)
    yield _ping()

    # Block lifecycle state.
    # current_block_type: the type of the currently open non-tool block
    #   ("thinking", "text", or None if no block is open).
    # next_block_index: the next content block index to assign.
    # tool_block_indexes: maps ToolCallDelta.index -> Anthropic block index.
    current_block_type: str | None = None
    current_block_index: int = -1
    next_block_index: int = 0
    tool_block_indexes: dict[int, int] = {}

    def _close_current_block() -> str | None:
        """Return a content_block_stop frame if a non-tool block is open."""
        nonlocal current_block_type
        if current_block_type is not None:
            current_block_type = None
            return _content_block_stop(current_block_index)
        return None

    try:
        async for event in events:
            if isinstance(event, ReasoningDelta):
                if current_block_type != "thinking":
                    close_frame = _close_current_block()
                    if close_frame:
                        yield close_frame
                    current_block_type = "thinking"
                    current_block_index = next_block_index
                    next_block_index += 1
                    yield _content_block_start(current_block_index, {
                        "type": "thinking",
                        "thinking": "",
                    })
                yield _content_block_delta(current_block_index, {
                    "type": "thinking_delta",
                    "thinking": event.content,
                })

            elif isinstance(event, ContentDelta):
                if current_block_type != "text":
                    close_frame = _close_current_block()
                    if close_frame:
                        yield close_frame
                    current_block_type = "text"
                    current_block_index = next_block_index
                    next_block_index += 1
                    yield _content_block_start(current_block_index, {
                        "type": "text",
                        "text": "",
                    })
                yield _content_block_delta(current_block_index, {
                    "type": "text_delta",
                    "text": event.content,
                })

            elif isinstance(event, ToolCallDelta):
                # Each unique ToolCallDelta.index gets its own tool_use block.
                # First delta for an index must carry call_id to open the
                # block (mirrors the OpenAI serializer's guard). Skip if
                # missing — nothing useful to emit.
                if event.index not in tool_block_indexes:
                    if not event.call_id:
                        continue
                    # Close any open non-tool block first.
                    close_frame = _close_current_block()
                    if close_frame:
                        yield close_frame
                    block_idx = next_block_index
                    next_block_index += 1
                    tool_block_indexes[event.index] = block_idx
                    yield _content_block_start(block_idx, {
                        "type": "tool_use",
                        "id": event.call_id,
                        "name": event.name or "",
                        "input": {},
                    })
                    if event.arguments_delta:
                        yield _content_block_delta(block_idx, {
                            "type": "input_json_delta",
                            "partial_json": event.arguments_delta,
                        })
                elif event.arguments_delta:
                    block_idx = tool_block_indexes[event.index]
                    yield _content_block_delta(block_idx, {
                        "type": "input_json_delta",
                        "partial_json": event.arguments_delta,
                    })

            elif isinstance(event, ToolResultEvent):
                # No Anthropic wire equivalent for tool results in the
                # assistant stream; skip silently.
                pass

            elif isinstance(event, StreamComplete):
                # Close any open non-tool block.
                close_frame = _close_current_block()
                if close_frame:
                    yield close_frame
                # Close any open tool blocks.
                for block_idx in tool_block_indexes.values():
                    yield _content_block_stop(block_idx)
                tool_block_indexes.clear()

                stop_reason = _map_stop_reason(event.finish_reason)
                yield _message_delta(stop_reason, event.metrics)
                yield _message_stop()
                return

    except Exception as exc:
        # Emit the error as a text content block, matching the OpenAI
        # serializer's pattern of surfacing errors to the consumer.
        close_frame = _close_current_block()
        if close_frame:
            yield close_frame
        for block_idx in tool_block_indexes.values():
            yield _content_block_stop(block_idx)
        tool_block_indexes.clear()

        err_index = next_block_index
        next_block_index += 1
        yield _content_block_start(err_index, {
            "type": "text",
            "text": "",
        })
        yield _content_block_delta(err_index, {
            "type": "text_delta",
            "text": f"[Error: {type(exc).__name__}: {exc}]",
        })
        yield _content_block_stop(err_index)

    # If we reach here without StreamComplete (source exhausted or errored),
    # still emit a clean message_delta + message_stop so consumers see a
    # well-formed stream.
    yield _message_delta("end_turn", StreamMetrics())
    yield _message_stop()
