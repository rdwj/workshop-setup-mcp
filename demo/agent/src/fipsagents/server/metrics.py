"""Prometheus metrics collector for the server layer.

Follows the TraceCollector observer pattern -- wraps async event streams,
yields events unchanged, and records Prometheus metrics as a side effect.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

from fipsagents.baseagent.events import (
    EventFailed,
    EventProcessed,
    EventReceived,
    SkillLearned,
    SkillRolledBack,
    StreamComplete,
    StreamEvent,
    ToolResultEvent,
    TrustLevelChanged,
    WorkItemCheckedOut,
    WorkItemCompleted,
)

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False


class MetricsCollector:
    """Records Prometheus metrics from StreamEvents.

    Requires ``prometheus_client`` (install via ``pip install 'fipsagents[metrics]'``).

    ``token_label_mode`` selects which label dimensions are attached to
    ``agent_tokens_total`` (see :class:`fipsagents.baseagent.config.MetricsConfig`).
    """

    _TOKEN_LABEL_DIMENSIONS = {
        "model": ("model", "direction"),
        "tenant": ("model", "direction", "tenant_id"),
        "session": ("model", "direction", "tenant_id", "session_id"),
    }

    def __init__(
        self,
        *,
        registry: Any = None,
        token_label_mode: str = "model",
    ) -> None:
        if not _HAS_PROMETHEUS:
            raise ImportError(
                "prometheus_client is required for MetricsCollector. "
                "Install with: pip install 'fipsagents[metrics]'"
            )
        if token_label_mode not in self._TOKEN_LABEL_DIMENSIONS:
            raise ValueError(
                f"Unknown token_label_mode {token_label_mode!r}; "
                f"expected one of {list(self._TOKEN_LABEL_DIMENSIONS)}"
            )
        self._token_label_mode = token_label_mode
        self._token_labelnames = list(
            self._TOKEN_LABEL_DIMENSIONS[token_label_mode]
        )
        self._registry = registry or CollectorRegistry()

        self.requests_total = Counter(
            "agent_requests_total",
            "Total chat completion requests",
            labelnames=["model", "status", "stream"],
            registry=self._registry,
        )
        self.request_duration = Histogram(
            "agent_request_duration_seconds",
            "Chat completion request duration",
            labelnames=["model"],
            registry=self._registry,
        )
        self.model_call_duration = Histogram(
            "agent_model_call_duration_seconds",
            "Individual model call duration",
            labelnames=["model"],
            registry=self._registry,
        )
        self.tool_calls_total = Counter(
            "agent_tool_call_total",
            "Total tool calls",
            labelnames=["tool_name", "status"],
            registry=self._registry,
        )
        self.tokens_total = Counter(
            "agent_tokens_total",
            "Total tokens processed",
            labelnames=self._token_labelnames,
            registry=self._registry,
        )
        self.events_received_total = Counter(
            "agent_events_received_total",
            "Total inbound events received",
            labelnames=["source", "event_type"],
            registry=self._registry,
        )
        self.events_processed_total = Counter(
            "agent_events_processed_total",
            "Total events processed",
            labelnames=["source", "event_type", "status"],
            registry=self._registry,
        )
        self.event_processing_duration = Histogram(
            "agent_event_processing_duration_seconds",
            "Event processing duration",
            labelnames=["source", "event_type"],
            registry=self._registry,
        )
        self.work_items_checked_out = Counter(
            "agent_work_items_checked_out_total",
            "Total work items checked out",
            registry=self._registry,
        )
        self.work_items_completed = Counter(
            "agent_work_items_completed_total",
            "Total work items completed",
            registry=self._registry,
        )
        self.work_item_duration = Histogram(
            "agent_work_item_duration_seconds",
            "Work item processing duration",
            registry=self._registry,
        )
        self.work_item_lease_expiries = Counter(
            "agent_work_item_lease_expiries_total",
            "Total work item lease expiries",
            registry=self._registry,
        )
        self.trust_level = Gauge(
            "agent_trust_level",
            "Current agent trust level (0-4)",
            registry=self._registry,
        )
        self.trust_score = Gauge(
            "agent_trust_score",
            "Current agent trust score",
            registry=self._registry,
        )
        self.trust_promotions = Counter(
            "agent_trust_promotions_total",
            "Trust level promotions",
            registry=self._registry,
        )
        self.trust_demotions = Counter(
            "agent_trust_demotions_total",
            "Trust level demotions",
            registry=self._registry,
        )
        self.skills_learned = Counter(
            "agent_skills_learned_total",
            "Skills learned or updated",
            registry=self._registry,
        )
        self.skills_rolled_back = Counter(
            "agent_skills_rolled_back_total",
            "Skills rolled back to prior version",
            registry=self._registry,
        )

    def _token_labels(
        self,
        *,
        model: str,
        direction: str,
        tenant_id: str | None,
        session_id: str | None,
    ) -> dict[str, str]:
        """Build the label kwargs for ``tokens_total`` for the active mode."""
        labels: dict[str, str] = {"model": model, "direction": direction}
        if "tenant_id" in self._token_labelnames:
            labels["tenant_id"] = tenant_id or "default"
        if "session_id" in self._token_labelnames:
            labels["session_id"] = session_id or "none"
        return labels

    async def observe(
        self,
        events: AsyncIterator[StreamEvent],
        *,
        model: str,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Wrap an event stream, recording metrics as events flow through.

        ``tenant_id`` and ``session_id`` are recorded as labels on
        ``agent_tokens_total`` only when ``token_label_mode`` includes them.
        ``None`` falls back to ``"default"`` / ``"none"`` so the label
        cardinality stays bounded even when the gateway omits the
        ``X-Tenant`` header or the request has no ``session_id``.
        """
        async for event in events:
            if isinstance(event, ToolResultEvent):
                status = "error" if event.is_error else "ok"
                self.tool_calls_total.labels(
                    tool_name=event.name,
                    status=status,
                ).inc()
            elif isinstance(event, EventReceived):
                self.events_received_total.labels(
                    source=event.source,
                    event_type=event.event_type,
                ).inc()
            elif isinstance(event, EventProcessed):
                self.events_processed_total.labels(
                    source=event.source,
                    event_type="",
                    status="ok",
                ).inc()
                self.event_processing_duration.labels(
                    source=event.source,
                    event_type="",
                ).observe(event.duration_ms / 1000.0)
            elif isinstance(event, EventFailed):
                self.events_processed_total.labels(
                    source=event.source,
                    event_type="",
                    status="failed",
                ).inc()
            elif isinstance(event, WorkItemCheckedOut):
                self.work_items_checked_out.inc()
            elif isinstance(event, WorkItemCompleted):
                self.work_items_completed.inc()
            elif isinstance(event, TrustLevelChanged):
                self.trust_level.set(event.to_level)
                self.trust_score.set(event.score)
                if event.to_level > event.from_level:
                    self.trust_promotions.inc()
                else:
                    self.trust_demotions.inc()
            elif isinstance(event, SkillLearned):
                self.skills_learned.inc()
            elif isinstance(event, SkillRolledBack):
                self.skills_rolled_back.inc()
            elif isinstance(event, StreamComplete):
                m = event.metrics
                if m.prompt_tokens is not None:
                    self.tokens_total.labels(
                        **self._token_labels(
                            model=model,
                            direction="prompt",
                            tenant_id=tenant_id,
                            session_id=session_id,
                        ),
                    ).inc(m.prompt_tokens)
                if m.completion_tokens is not None:
                    self.tokens_total.labels(
                        **self._token_labels(
                            model=model,
                            direction="completion",
                            tenant_id=tenant_id,
                            session_id=session_id,
                        ),
                    ).inc(m.completion_tokens)
                if m.total_time > 0:
                    self.model_call_duration.labels(model=model).observe(
                        m.total_time,
                    )
            yield event

    def record_request_start(self) -> float:
        """Mark the start of a request. Returns monotonic timestamp."""
        return time.monotonic()

    def record_request_end(
        self,
        model: str,
        stream: bool,
        status: str,
        start_time: float,
    ) -> None:
        """Record request completion metrics."""
        duration = time.monotonic() - start_time
        self.requests_total.labels(
            model=model,
            status=status,
            stream=str(stream).lower(),
        ).inc()
        self.request_duration.labels(model=model).observe(duration)

    def generate_metrics(self) -> bytes:
        """Return Prometheus text exposition format."""
        return generate_latest(self._registry)


class NullMetricsCollector:
    """No-op metrics collector when prometheus_client is not installed."""

    async def observe(
        self,
        events: AsyncIterator[StreamEvent],
        *,
        model: str,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        async for event in events:
            yield event

    def record_request_start(self) -> float:
        return time.monotonic()

    def record_request_end(
        self,
        model: str,
        stream: bool,
        status: str,
        start_time: float,
    ) -> None:
        pass

    def generate_metrics(self) -> bytes:
        return b""


def create_metrics_collector(
    enabled: bool = False,
    *,
    token_label_mode: str = "model",
) -> MetricsCollector | NullMetricsCollector:
    """Create a metrics collector based on config."""
    if not enabled:
        return NullMetricsCollector()
    if not _HAS_PROMETHEUS:
        logger.warning(
            "Metrics enabled but prometheus_client not installed. "
            "Install with: pip install 'fipsagents[metrics]'"
        )
        return NullMetricsCollector()
    return MetricsCollector(token_label_mode=token_label_mode)
