"""OpenTelemetry trace export backend.

Wraps an inner :class:`TraceStore` and exports traces to an OTLP
collector. Query methods (``get_trace``, ``list_traces``) delegate
to the inner store; ``save_trace`` writes to both.

Requires the ``[otel]`` extra: ``pip install 'fipsagents[otel]'``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from .propagation import _string_to_span_id, _string_to_trace_id
from .tracing import NullTraceStore, Trace, TraceSummary, TraceStore

logger = logging.getLogger(__name__)


class OTELTraceStore(TraceStore):
    """Trace store that exports to an OpenTelemetry collector.

    Wraps an inner ``TraceStore`` so that query endpoints
    (``get_trace``, ``list_traces``) still work. ``save_trace``
    persists to the inner store first, then exports spans via OTLP.

    Args:
        endpoint: OTLP gRPC endpoint (e.g. ``http://localhost:4317``).
            Falls back to ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var.
        inner: Inner store for query/persistence. Defaults to
            ``NullTraceStore``.
        service_name: Service name for the OTEL resource.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        inner: TraceStore | None = None,
        service_name: str = "fipsagents",
    ) -> None:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        self._inner = inner or NullTraceStore()
        self._endpoint = (
            endpoint
            or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or "http://localhost:4317"
        )

        resource = Resource.create({"service.name": service_name})
        self._provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=self._endpoint, insecure=True)
        self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("fipsagents")
        logger.info(
            "OTELTraceStore: exporting to %s (service=%s)",
            self._endpoint, service_name,
        )

    def _convert_spans(self, trace: Trace) -> None:
        """Translate internal Spans to OTEL spans and export.

        Uses the tracer's start_span to build the span tree.
        Since OTEL SDK spans are created and ended synchronously, this
        does not block the event loop.
        """
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import SpanContext, TraceFlags

        if not trace.spans:
            return

        # Anchor for monotonic -> wall-clock conversion.
        anchor_dt = datetime.fromisoformat(trace.started_at)
        anchor_mono = trace.spans[0].start_time

        def _wall_ns(mono: float) -> int:
            """Convert monotonic time to wall-clock nanoseconds."""
            offset = mono - anchor_mono
            wall = anchor_dt.timestamp() + offset
            return int(wall * 1_000_000_000)

        # Build OTEL spans. Process in order so parents exist before children.
        otel_trace_id = _string_to_trace_id(trace.trace_id)

        for span in trace.spans:
            # Build parent context. Every span needs one so the SDK
            # inherits our deterministic trace ID instead of generating
            # a random one.
            if span.parent_span_id:
                parent_otel_id = _string_to_span_id(span.parent_span_id)
            else:
                # Root span: use a synthetic parent that carries the
                # trace ID but won't appear in the exported tree.
                parent_otel_id = otel_trace.INVALID_SPAN_ID
            parent_span_ctx = SpanContext(
                trace_id=otel_trace_id,
                span_id=parent_otel_id,
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            parent_ctx = otel_trace.set_span_in_context(
                otel_trace.NonRecordingSpan(parent_span_ctx),
            )

            # Create and populate the span.
            otel_span = self._tracer.start_span(
                name=span.name or "unknown",
                context=parent_ctx,
                start_time=_wall_ns(span.start_time),
            )

            # Set attributes.
            for key, value in span.attributes.items():
                if isinstance(value, (str, int, float, bool)):
                    otel_span.set_attribute(key, value)

            # Export span events.
            for evt in span.events:
                evt_attrs: dict[str, object] = {}
                if "body" in evt:
                    body_val = evt["body"]
                    if isinstance(body_val, str) and len(body_val) <= 65536:
                        evt_attrs["body"] = body_val
                    elif isinstance(body_val, str):
                        evt_attrs["body"] = body_val[:65536]
                evt_ts = evt.get("timestamp")
                ts_ns = _wall_ns(evt_ts) if evt_ts is not None else None
                otel_span.add_event(
                    evt.get("name", "unknown"),
                    attributes=evt_attrs,
                    timestamp=ts_ns,
                )

            # Set status.
            if span.status == "error":
                otel_span.set_status(
                    otel_trace.StatusCode.ERROR, "Agent error",
                )

            # End the span.
            end_ns = (
                _wall_ns(span.end_time)
                if span.end_time is not None
                else _wall_ns(span.start_time)
            )
            otel_span.end(end_time=end_ns)

    async def save_trace(self, trace: Trace) -> None:
        """Persist to inner store, then export to OTEL."""
        await self._inner.save_trace(trace)
        try:
            self._convert_spans(trace)
        except Exception:
            logger.warning(
                "OTELTraceStore: failed to export trace %s",
                trace.trace_id, exc_info=True,
            )

    async def get_trace(self, trace_id: str) -> Trace | None:
        return await self._inner.get_trace(trace_id)

    async def list_traces(
        self, *, limit: int = 50, offset: int = 0,
    ) -> list[TraceSummary]:
        return await self._inner.list_traces(limit=limit, offset=offset)

    async def delete_before(self, cutoff: datetime) -> int:
        return await self._inner.delete_before(cutoff)

    async def close(self) -> None:
        """Flush pending spans and shut down the provider."""
        try:
            self._provider.force_flush()
            self._provider.shutdown()
        except Exception:
            logger.warning("OTELTraceStore: error during shutdown", exc_info=True)
        await self._inner.close()
