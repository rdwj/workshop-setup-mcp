"""Request/response models and helpers for the OpenAI-compatible server."""

from __future__ import annotations

import re
import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from fipsagents.baseagent.events import StreamMetrics

# Session ID format: 1-128 alphanumeric characters, hyphens, or underscores.
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


# ---------------------------------------------------------------------------
# Request / response schema
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    """Request body for POST /v1/sessions."""

    session_id: str | None = None

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, v: str | None) -> str | None:
        if v is not None and not _SESSION_ID_RE.match(v):
            raise ValueError(
                "session_id must be 1-128 characters: "
                "letters, digits, hyphens, or underscores"
            )
        return v


class TextBlock(BaseModel):
    """OpenAI-shaped text content block."""

    type: Literal["text"]
    text: str


class ImageUrl(BaseModel):
    """Inner ``image_url`` payload for :class:`ImageUrlBlock`.

    ``url`` accepts a remote ``https://`` URL, an inline ``data:`` URI, or
    the internal ``file_id:<id>`` scheme that the server resolves against
    its :class:`~fipsagents.server.bytes_store.BytesStore` before
    forwarding the request to the model.
    """

    url: str
    detail: Literal["auto", "low", "high"] | None = None


class ImageUrlBlock(BaseModel):
    """OpenAI-shaped image content block."""

    type: Literal["image_url"]
    image_url: ImageUrl


ContentBlock = Annotated[TextBlock | ImageUrlBlock, Field(discriminator="type")]


class ChatMessage(BaseModel):
    role: str
    content: str | list[ContentBlock] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(..., min_length=1)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    # vLLM-specific parameters — forwarded via extra_body.
    top_k: int | None = None
    repetition_penalty: float | None = None
    reasoning_effort: str | None = None
    # Session persistence (extension field, not part of OpenAI API).
    session_id: str | None = Field(
        default=None,
        description="Session ID for conversation persistence. "
        "If provided but no session exists, one is created automatically.",
    )
    answers_to_question_id: str | None = Field(
        default=None,
        description="ID of a pending question this request answers.",
    )
    # File attachments (extension field, not part of OpenAI API).
    file_ids: list[str] | None = Field(
        default=None,
        description="IDs of files previously uploaded via POST /v1/files. "
        "The server fetches each file's extracted text from the FileStore "
        "and injects it as additional context before processing the user "
        "message. Files referenced but unparsed are injected as a stub "
        "noting filename + parse_status.",
    )

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, v: str | None) -> str | None:
        if v is not None and not _SESSION_ID_RE.match(v):
            raise ValueError(
                "session_id must be 1-128 characters: "
                "letters, digits, hyphens, or underscores"
            )
        return v


class CreateFeedbackRequest(BaseModel):
    """Request body for POST /v1/feedback.

    ``trace_id`` is optional: clients normally send the value they
    received from the chat completion (in the ``X-Trace-Id`` response
    header or the final ``trace_id`` field of the SSE usage chunk), but
    when tracing is sampled out or the caller does not have one the
    server synthesises a stand-alone identifier. Records keyed to a
    real trace can be joined to the trace store; orphan records
    cannot but are still useful as raw rating data.
    """

    rating: int
    trace_id: str | None = None
    session_id: str | None = None
    comment: str | None = None
    correction: str | None = None
    model_id: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    turn_index: int | None = Field(default=None, ge=0)
    agent_type: str | None = None

    @field_validator("rating")
    @classmethod
    def _validate_rating(cls, v: int) -> int:
        if v not in (1, -1):
            raise ValueError("rating must be 1 (thumbs-up) or -1 (thumbs-down)")
        return v


class ForkSessionRequest(BaseModel):
    """Request body for POST /v1/sessions/{session_id}/fork."""

    from_message_index: int | None = None


class ForkSessionResponse(BaseModel):
    """Response body for POST /v1/sessions/{session_id}/fork."""

    session_id: str
    parent_session_id: str
    message_count: int


class RevertSessionRequest(BaseModel):
    """Request body for POST /v1/sessions/{session_id}/revert."""

    to_message_index: int = Field(ge=0)


class UpdateFeedbackRequest(BaseModel):
    """Request body for PATCH /v1/feedback/{feedback_id}.

    All fields are optional — omit a field to leave it unchanged. When
    ``rating`` is supplied it must be 1 or -1, same as create.
    """

    rating: int | None = None
    comment: str | None = None
    correction: str | None = None

    @field_validator("rating")
    @classmethod
    def _validate_rating(cls, v: int | None) -> int | None:
        if v is not None and v not in (1, -1):
            raise ValueError("rating must be 1 (thumbs-up) or -1 (thumbs-down)")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _messages_to_dicts(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Convert incoming Pydantic messages back to OpenAI-shaped dicts.

    Multimodal content (a list of :class:`TextBlock` / :class:`ImageUrlBlock`)
    is dumped block-by-block so the OpenAI SDK receives plain dicts.
    ``exclude_none=True`` so optional fields the caller omitted (notably
    ``image_url.detail``) stay absent rather than being serialised as
    explicit ``null`` — the OpenAI SDK's
    ``ChatCompletionContentPartImageParam`` rejects ``null`` for
    ``detail`` even though the field itself is optional.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role}
        if m.content is not None:
            if isinstance(m.content, list):
                d["content"] = [b.model_dump(exclude_none=True) for b in m.content]
            else:
                d["content"] = m.content
        if m.tool_calls is not None:
            d["tool_calls"] = m.tool_calls
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        out.append(d)
    return out


def _extract_overrides(req: ChatCompletionRequest) -> dict[str, Any]:
    """Extract non-None sampling parameters from the request.

    Standard OpenAI parameters go at the top level. vLLM-specific parameters
    (top_k, repetition_penalty, reasoning_effort) are placed inside
    ``extra_body`` so the openai SDK forwards them without validation errors.
    """
    overrides: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}

    # Standard OpenAI parameters.
    for key in (
        "temperature",
        "max_tokens",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "logprobs",
        "top_logprobs",
    ):
        val = getattr(req, key, None)
        if val is not None:
            overrides[key] = val

    # vLLM-specific parameters — must go via extra_body.
    for key in ("top_k", "repetition_penalty", "reasoning_effort"):
        val = getattr(req, key, None)
        if val is not None:
            extra_body[key] = val

    if extra_body:
        overrides["extra_body"] = extra_body

    return overrides


def _sync_response(
    model_name: str,
    content: str,
    *,
    metrics: StreamMetrics | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    m = metrics or StreamMetrics()
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": m.prompt_tokens or 0,
            "completion_tokens": m.completion_tokens or 0,
            "total_tokens": m.total_tokens or (
                (m.prompt_tokens or 0) + (m.completion_tokens or 0)
            ),
        },
        "stream_metrics": {
            "time_to_first_reasoning": m.time_to_first_reasoning,
            "time_to_first_content": m.time_to_first_content,
            "total_time": m.total_time,
            "inter_token_latencies": m.inter_token_latencies,
            "model_calls": m.model_calls,
            "tool_calls": m.tool_calls,
        },
    }
