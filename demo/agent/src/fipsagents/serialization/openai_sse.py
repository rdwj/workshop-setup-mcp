"""Serialize a ``BaseAgent.astep_stream()`` event sequence to OpenAI Chat
Completions streaming format (SSE).

Wire format reference:
https://platform.openai.com/docs/api-reference/chat/streaming

Each SSE frame is a single ``data: <json>\\n\\n`` line. The stream ends with
``data: [DONE]\\n\\n``.  Each JSON payload has the shape::

    {
      "id": "chatcmpl-<hex>",
      "object": "chat.completion.chunk",
      "created": <unix timestamp>,
      "model": "<model name>",
      "choices": [{"index": 0, "delta": {...}, "finish_reason": null}]
    }

The public entry point is :func:`stream_events_as_sse`. It is a pure async
generator — no FastAPI, no logging, no side effects. Callers own the transport
and any logging they want to add around iteration.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from fipsagents.baseagent.events import (
    CompactionCompleted,
    CompactionSkipped,
    CompactionStarted,
    ContentDelta,
    LimitExceeded,
    LoopBreakEvent,
    PermissionDecisionMade,
    QuestionAnswered,
    QuestionAsked,
    ReasoningDelta,
    StreamComplete,
    StreamEvent,
    StreamMetrics,
    SubagentCompleted,
    SubagentDelta,
    SubagentFailed,
    SubagentInvoked,
    ToolCallDelta,
    ToolResultEvent,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _make_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _now() -> int:
    return int(time.time())


def _sse_chunk(
    completion_id: str,
    model_name: str,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    """Serialize one OpenAI stream chunk as a single SSE ``data:`` frame."""
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _usage_chunk(
    completion_id: str,
    model_name: str,
    metrics: StreamMetrics,
    trace_id: str | None = None,
) -> str:
    """Serialize a final usage chunk (OpenAI ``include_usage`` convention).

    Shape matches OpenAI's ``stream_options: {include_usage: true}``
    behaviour — a chunk with ``choices: []`` and a top-level ``usage``
    object. We also attach a ``stream_metrics`` extension carrying
    TTFT / ITL / counters that OpenAI's spec does not cover; unknown
    fields are ignored by conforming clients.

    When ``trace_id`` is provided it is included as a top-level field so
    clients can correlate this completion with a stored trace (e.g. to
    attach feedback via ``POST /v1/feedback``).
    """
    chunk: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model_name,
        "choices": [],
        "usage": {
            "prompt_tokens": metrics.prompt_tokens,
            "completion_tokens": metrics.completion_tokens,
            "total_tokens": metrics.total_tokens,
        },
        "stream_metrics": {
            "time_to_first_reasoning": metrics.time_to_first_reasoning,
            "time_to_first_content": metrics.time_to_first_content,
            "total_time": metrics.total_time,
            "inter_token_latencies": metrics.inter_token_latencies,
            "model_calls": metrics.model_calls,
            "tool_calls": metrics.tool_calls,
        },
    }
    if trace_id is not None:
        chunk["trace_id"] = trace_id
    return f"data: {json.dumps(chunk)}\n\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def stream_events_as_sse(
    events: AsyncIterator[StreamEvent],
    model_name: str,
    completion_id: str | None = None,
    trace_id: str | None = None,
) -> AsyncIterator[str]:
    """Translate a ``StreamEvent`` sequence into OpenAI SSE chunks.

    Args:
        events: Async iterator of ``StreamEvent`` instances, typically from
            ``BaseAgent.astep_stream()``.
        model_name: Model identifier echoed in every chunk's ``model`` field.
        completion_id: Optional completion ID. When ``None`` an ID of the form
            ``chatcmpl-<24 hex chars>`` is generated automatically.
        trace_id: Optional trace identifier. When provided it is attached to
            the final usage chunk so clients can correlate the completion
            with a stored trace (e.g. for feedback submission).

    Yields:
        SSE-encoded strings (``data: {...}\\n\\n`` or ``data: [DONE]\\n\\n``).
        On exception from the source iterator an error chunk is yielded before
        ``[DONE]``.
    """
    if completion_id is None:
        completion_id = _make_completion_id()

    # OpenAI convention: lead with a role chunk so clients that key off the
    # first role they see don't misfire their "finalize message" logic on the
    # first content token.
    yield _sse_chunk(completion_id, model_name, {"role": "assistant"})

    # Per-call emission state: tracks which tool-call IDs have already
    # received their opening chunk (carrying id + name).  Keyed by
    # call_id (not index) so that a second model iteration reusing
    # index 0 with a new call_id still gets a proper opening chunk.
    opened_call_ids: set[str] = set()

    try:
        async for event in events:
            if isinstance(event, ReasoningDelta):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {"reasoning_content": event.content},
                )

            elif isinstance(event, ContentDelta):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {"content": event.content},
                )

            elif isinstance(event, ToolCallDelta):
                # First delta for a given call_id carries id + name.
                # Later deltas carry only the arguments fragment.
                # Skip deltas with neither a call_id (first) nor an
                # arguments_delta (continuation) — nothing to emit.
                if event.call_id and event.call_id not in opened_call_ids:
                    opened_call_ids.add(event.call_id)
                    yield _sse_chunk(
                        completion_id,
                        model_name,
                        {
                            "tool_calls": [
                                {
                                    "index": event.index,
                                    "id": event.call_id,
                                    "type": "function",
                                    "function": {
                                        "name": event.name or "",
                                        "arguments": event.arguments_delta,
                                    },
                                }
                            ]
                        },
                    )
                elif event.arguments_delta:
                    yield _sse_chunk(
                        completion_id,
                        model_name,
                        {
                            "tool_calls": [
                                {
                                    "index": event.index,
                                    "function": {
                                        "arguments": event.arguments_delta,
                                    },
                                }
                            ]
                        },
                    )

            elif isinstance(event, ToolResultEvent):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "role": "tool",
                        "tool_call_id": event.call_id,
                        "content": event.content,
                    },
                )

            elif isinstance(event, SubagentInvoked):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "subagent": {
                            "type": "invoked",
                            "agent_name": event.agent_name,
                            "task": event.task,
                            "span_id": event.span_id,
                            "transport": event.transport,
                            "depth": event.depth,
                        }
                    },
                )

            elif isinstance(event, SubagentCompleted):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "subagent": {
                            "type": "completed",
                            "agent_name": event.agent_name,
                            "span_id": event.span_id,
                            "content": event.content,
                            "tokens_used": event.tokens_used,
                            "tool_calls_made": event.tool_calls_made,
                            "cost_usd": event.cost_usd,
                        }
                    },
                )

            elif isinstance(event, SubagentFailed):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "subagent": {
                            "type": "failed",
                            "agent_name": event.agent_name,
                            "span_id": event.span_id,
                            "error_type": event.error_type,
                            "error_message": event.error_message,
                        }
                    },
                )

            elif isinstance(event, SubagentDelta):
                # v1: forward-compat placeholder. Delta nested in subagent object.
                # v2 will recursively serialize event.delta with the full event stream.
                # For now, emit repr() of the delta for debugging; v2 will refactor.
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "subagent": {
                            "type": "delta",
                            "agent_name": event.agent_name,
                            "span_id": event.span_id,
                            "delta": repr(event.delta),  # v1 placeholder
                        }
                    },
                )

            elif isinstance(event, CompactionStarted):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "compaction": {
                            "type": "started",
                            "session_id": event.session_id,
                            "message_count": event.message_count,
                        }
                    },
                )

            elif isinstance(event, CompactionCompleted):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "compaction": {
                            "type": "completed",
                            "session_id": event.session_id,
                            "original_count": event.original_count,
                            "compacted_count": event.compacted_count,
                        }
                    },
                )

            elif isinstance(event, CompactionSkipped):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "compaction": {
                            "type": "skipped",
                            "reason": event.reason,
                            "session_id": event.session_id,
                        }
                    },
                )

            elif isinstance(event, PermissionDecisionMade):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "permission": {
                            "tool": event.tool,
                            "action": event.action,
                            "rule_id": event.rule_id,
                            "scope": event.scope,
                        }
                    },
                )

            elif isinstance(event, QuestionAsked):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "question": {
                            "type": "asked",
                            "question_id": event.question_id,
                            "question_text": event.question_text,
                            "options": event.options,
                            "multiple": event.multiple,
                            "allow_custom": event.allow_custom,
                            "session_id": event.session_id,
                        }
                    },
                )

            elif isinstance(event, QuestionAnswered):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "question": {
                            "type": "answered",
                            "question_id": event.question_id,
                            "answer_text": event.answer_text,
                        }
                    },
                )

            elif isinstance(event, LimitExceeded):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "limit": {
                            "type": "exceeded",
                            "limit_type": event.limit_type,
                            "threshold": event.threshold,
                            "actual": event.actual,
                        }
                    },
                )

            elif isinstance(event, LoopBreakEvent):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {
                        "loop": {
                            "type": "break",
                            "tool_name": event.tool_name,
                            "repeat_count": event.repeat_count,
                            "last_args": event.last_args,
                            "last_error": event.last_error,
                        }
                    },
                )

            elif isinstance(event, StreamComplete):
                yield _sse_chunk(
                    completion_id,
                    model_name,
                    {},
                    finish_reason=event.finish_reason,
                )
                # OpenAI's stream_options.include_usage appends a
                # separate chunk with empty choices carrying usage.
                yield _usage_chunk(
                    completion_id, model_name, event.metrics, trace_id=trace_id,
                )

    except Exception as exc:
        err = {"error": {"message": str(exc), "type": type(exc).__name__}}
        yield f"data: {json.dumps(err)}\n\n"

    yield "data: [DONE]\n\n"
