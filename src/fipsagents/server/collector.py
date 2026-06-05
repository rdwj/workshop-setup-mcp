"""TraceCollector -- observes StreamEvents and builds trace spans.

Wraps an async event stream from ``BaseAgent.astep_stream()`` and
builds a tree of :class:`~fipsagents.server.tracing.Span` objects
as events flow through.  Events are yielded unchanged -- this is a
pure observer that adds no latency to the response stream.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fipsagents.baseagent.events import (
    ContentDelta,
    EventReceived,
    ReasoningDelta,
    StreamComplete,
    StreamEvent,
    StreamMetrics,
    ToolCallDelta,
    ToolResultEvent,
)

from .tracing import Span, Trace, TraceStore, _utc_now_iso

logger = logging.getLogger(__name__)


class TraceCollector:
    """Observes StreamEvents and builds trace spans.

    Usage in the server::

        collector = TraceCollector(store, trace_id=request_id)
        collector.begin_request({"model": model_name, "stream": True})
        events = agent.astep_stream(**overrides)
        observed = collector.observe(events)
        async for chunk in stream_events_as_sse(observed, model_name):
            yield chunk
        await collector.end_request()
    """

    def __init__(
        self,
        store: TraceStore,
        *,
        trace_id: str | None = None,
        parent_trace_id: str | None = None,
        parent_span_id: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        fidelity: str = "minimal",
    ) -> None:
        self.store = store
        # If a parent trace context is provided, use its trace ID
        # so this trace joins the distributed trace.
        self.trace_id = parent_trace_id or trace_id or f"trace_{uuid.uuid4().hex[:16]}"
        self._parent_span_id = parent_span_id
        self._session_id = session_id
        self._model = model
        # OTEL GenAI semantic convention: gen_ai.system identifies the
        # provider (eg "openai", "anthropic", "vllm") so trace consumers
        # can group spans across providers without parsing model names.
        self._provider = provider
        self._fidelity = fidelity

        self._spans: list[Span] = []
        self._request_span: Span | None = None
        self._current_step_span: Span | None = None
        self._current_model_span: Span | None = None
        self._step_count = 0
        self._pending_tool_spans: dict[str, Span] = {}  # call_id -> Span
        self._needs_new_step = False
        self._started_at: str | None = None

    # ------------------------------------------------------------------
    # Span helpers
    # ------------------------------------------------------------------

    def _make_span(
        self, name: str, parent_id: str | None = None, **attrs: Any
    ) -> Span:
        span = Span(
            trace_id=self.trace_id,
            span_id=f"span_{uuid.uuid4().hex[:16]}",
            parent_span_id=parent_id,
            name=name,
            start_time=time.monotonic(),
            attributes=dict(attrs) if attrs else {},
        )
        self._spans.append(span)
        return span

    @staticmethod
    def _end_span(span: Span) -> None:
        if span.end_time is None:
            span.end_time = time.monotonic()

    def _record_span_event(self, span: Span, name: str, body: Any) -> None:
        """Append an event to span.events with a monotonic timestamp."""
        span.events.append({
            "name": name,
            "timestamp": time.monotonic(),
            "body": body if isinstance(body, str) else json.dumps(body, default=str),
        })

    # ------------------------------------------------------------------
    # Request lifecycle
    # ------------------------------------------------------------------

    def begin_request(
        self,
        attributes: dict[str, Any] | None = None,
        *,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Open the root request span and the first step.

        Stamps OTEL GenAI semantic-convention attributes on the request
        span (`gen_ai.operation.name`, `gen_ai.request.model`,
        `gen_ai.system`) so trace consumers can recognise this as a
        chat-completion regardless of how the underlying spans are named.
        """
        self._started_at = _utc_now_iso()
        attrs = dict(attributes or {})
        attrs.setdefault("gen_ai.operation.name", "chat")
        if self._model:
            attrs.setdefault("gen_ai.request.model", self._model)
        if self._provider:
            attrs.setdefault("gen_ai.system", self._provider)
        self._request_span = self._make_span(
            "request", parent_id=self._parent_span_id, **attrs,
        )
        if self._fidelity != "minimal" and messages is not None:
            self._record_span_event(
                self._request_span, "messages_snapshot", messages,
            )
        self._begin_step()

    def _begin_step(self) -> None:
        self._step_count += 1
        parent_id = (
            self._request_span.span_id if self._request_span else None
        )
        self._current_step_span = self._make_span(
            f"step:{self._step_count}", parent_id=parent_id
        )
        self._begin_model_call()

    def _end_step(self) -> None:
        if self._current_model_span is not None:
            self._end_model_call()
        if self._current_step_span is not None:
            self._end_span(self._current_step_span)
            self._current_step_span = None

    def _begin_model_call(self) -> None:
        parent_id = (
            self._current_step_span.span_id
            if self._current_step_span
            else None
        )
        self._current_model_span = self._make_span(
            "model_call", parent_id=parent_id
        )

    def _end_model_call(self, metrics: StreamMetrics | None = None) -> None:
        """Close the model_call span, stamping token counts as attributes.

        Both the legacy attribute names (``prompt_tokens`` /
        ``completion_tokens`` / ``total_tokens``) and the OTEL GenAI
        semantic-convention names (``gen_ai.usage.input_tokens`` /
        ``gen_ai.usage.output_tokens``) are emitted, so existing trace
        consumers keep working while OTEL backends (Tempo, Honeycomb,
        Grafana Cloud) get the standard attribute keys they expect.
        """
        if self._current_model_span is None:
            return
        if metrics is not None:
            attrs = self._current_model_span.attributes
            if metrics.prompt_tokens is not None:
                attrs["prompt_tokens"] = metrics.prompt_tokens
                attrs["gen_ai.usage.input_tokens"] = metrics.prompt_tokens
            if metrics.completion_tokens is not None:
                attrs["completion_tokens"] = metrics.completion_tokens
                attrs["gen_ai.usage.output_tokens"] = metrics.completion_tokens
            if metrics.total_tokens is not None:
                attrs["total_tokens"] = metrics.total_tokens
            if self._model:
                attrs.setdefault("gen_ai.request.model", self._model)
                attrs.setdefault("gen_ai.response.model", self._model)
            if self._provider:
                attrs.setdefault("gen_ai.system", self._provider)
            attrs["total_time"] = metrics.total_time
        self._end_span(self._current_model_span)
        self._current_model_span = None

    # ------------------------------------------------------------------
    # Event observation
    # ------------------------------------------------------------------

    async def observe(
        self, events: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[StreamEvent]:
        """Wrap *events*, building spans as a side effect.

        Every event is yielded unchanged.  Tracing errors are logged at
        warning level but never propagated -- tracing must not break the
        response stream.
        """
        async for event in events:
            try:
                self._process_event(event)
            except Exception:
                logger.warning(
                    "TraceCollector: error processing %s",
                    type(event).__name__,
                    exc_info=True,
                )
            yield event

    def _process_event(self, event: StreamEvent) -> None:
        """Dispatch a single event to the appropriate span logic."""
        if isinstance(event, (ContentDelta, ReasoningDelta)):
            self._maybe_start_new_step()
            if self._fidelity == "full" and self._current_model_span is not None:
                if isinstance(event, ContentDelta):
                    self._record_span_event(self._current_model_span, "content_delta", event.content)
                else:
                    self._record_span_event(self._current_model_span, "reasoning_delta", event.content)

        elif isinstance(event, ToolCallDelta):
            self._maybe_start_new_step()
            self._handle_tool_call_delta(event)
            if self._fidelity == "full" and self._current_model_span is not None:
                self._record_span_event(self._current_model_span, "tool_call_delta", {
                    "index": event.index,
                    "call_id": event.call_id,
                    "name": event.name,
                    "arguments_delta": event.arguments_delta,
                })

        elif isinstance(event, ToolResultEvent):
            if self._fidelity != "minimal":
                tool_span = self._pending_tool_spans.get(event.call_id)
                if tool_span is not None:
                    self._record_span_event(tool_span, "tool_result", {
                        "content": event.content[:16384],
                        "is_error": event.is_error,
                    })
            self._handle_tool_result(event)

        elif isinstance(event, EventReceived):
            if self._request_span:
                self._request_span.attributes["event_id"] = event.event_id
                self._request_span.attributes["event_type"] = event.event_type
                self._request_span.attributes["event_source"] = event.source

        elif isinstance(event, StreamComplete):
            self._handle_stream_complete(event)

    def _maybe_start_new_step(self) -> None:
        """Start a new step if we are between agent loop iterations."""
        if self._needs_new_step:
            self._needs_new_step = False
            self._begin_step()

    def _handle_tool_call_delta(self, event: ToolCallDelta) -> None:
        """Track tool call spans, keyed by call_id."""
        if event.call_id is None or event.call_id in self._pending_tool_spans:
            return  # continuation delta or already tracked
        parent_id = (
            self._current_step_span.span_id
            if self._current_step_span
            else None
        )
        tool_name = event.name or "unknown"
        span = self._make_span(f"tool:{tool_name}", parent_id=parent_id)
        span.attributes["tool_name"] = tool_name
        self._pending_tool_spans[event.call_id] = span

    def _handle_tool_result(self, event: ToolResultEvent) -> None:
        """Close the tool span and detect step boundaries."""
        span = self._pending_tool_spans.pop(event.call_id, None)
        if span is not None:
            self._end_span(span)
            span.attributes["tool_name"] = event.name
            span.attributes["content_length"] = len(event.content)
            span.attributes["is_error"] = event.is_error
            if event.is_error:
                span.status = "error"

        # When all pending tool calls have completed, the current step
        # is done and the agent will loop for another model call.
        if not self._pending_tool_spans:
            self._end_step()
            self._needs_new_step = True

    def _handle_stream_complete(self, event: StreamComplete) -> None:
        """Finalize all open spans on stream termination."""
        self._end_model_call(event.metrics)

        # If we were expecting a new step (tools just finished) but
        # the stream ended instead, that flag is stale -- clear it.
        self._needs_new_step = False

        if self._current_step_span is not None:
            self._end_step()

        # Close any orphaned tool spans (shouldn't happen in normal
        # flow, but be defensive).
        for span in self._pending_tool_spans.values():
            self._end_span(span)
            span.status = "error"
            span.attributes["orphaned"] = True
        self._pending_tool_spans.clear()

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    async def end_request(self, status: str = "ok") -> None:
        """Close the root span and persist the trace."""
        ended_at = _utc_now_iso()

        if self._request_span is not None:
            self._end_span(self._request_span)
            self._request_span.status = status

        trace = Trace(
            trace_id=self.trace_id,
            started_at=self._started_at or ended_at,
            ended_at=ended_at,
            model=self._model,
            session_id=self._session_id,
            status=status,
            spans=list(self._spans),
        )

        try:
            await self.store.save_trace(trace)
        except Exception:
            logger.warning(
                "TraceCollector: failed to persist trace %s",
                self.trace_id,
                exc_info=True,
            )

        logger.debug(
            "Trace %s: %d spans, status=%s",
            self.trace_id,
            len(self._spans),
            status,
        )
