"""Event-triggered mode ABCs, models, and factories.

Event sources yield inbound events that the server translates into
conversation messages and feeds to the agent. Event sinks emit outbound
events (e.g. log, HTTP callback) after the agent processes each event.

``NullEventSource`` (default) never yields events -- fully
backward-compatible. ``NullSink`` discards all events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from fipsagents.baseagent.config import EventRetryConfig as RetryConfig  # noqa: F401

logger = logging.getLogger(__name__)


# -- Models ----------------------------------------------------------------


class InboundEvent(BaseModel):
    """An event received from an external source."""

    event_id: str
    event_type: str
    payload: dict[str, Any]
    source: str
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_key: str | None = None


class OutboundEvent(BaseModel):
    """An event emitted after processing."""

    correlation_id: str
    event_type: str
    payload: dict[str, Any]
    source: str
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)



# -- ABCs ------------------------------------------------------------------


class EventSource(ABC):
    """Base class for event sources.

    Subclasses implement ``consume()`` as an async generator that yields
    :class:`InboundEvent` instances. The server calls ``setup()`` once
    before consuming, and ``close()`` when shutting down.
    """

    def __init__(self, source_id: str, *, config: Any = None) -> None:
        self.source_id = source_id
        self.config = config

    async def setup(self, **kwargs: Any) -> None:
        """Initialize the source. Webhook sources receive ``app=`` here."""

    @abstractmethod
    async def consume(self) -> AsyncIterator[InboundEvent]:
        """Yield events. Subclasses must use ``async def`` with ``yield``."""
        ...  # pragma: no cover
        # Make this a generator so the type checker is happy
        if False:  # type: ignore[unreachable]
            yield  # type: ignore[misc]

    async def acknowledge(self, event_id: str) -> None:
        """Acknowledge event processing. Default no-op."""

    async def close(self) -> None:
        """Release resources. Default no-op."""


class EventSink(ABC):
    """Base class for event sinks.

    Subclasses implement ``emit()`` to send processed events somewhere
    (logs, HTTP callback, message queue, etc.).
    """

    async def setup(self) -> None:
        """Initialize the sink."""

    @abstractmethod
    async def emit(self, event: OutboundEvent) -> None:
        """Emit a processed event."""
        ...  # pragma: no cover

    async def close(self) -> None:
        """Release resources. Default no-op."""


# -- Translation -----------------------------------------------------------


def default_translate_event(event: InboundEvent) -> list[dict[str, str]]:
    """Convert an inbound event into conversation messages.

    Server-layer function -- NOT a BaseAgent method. The server calls
    this to build the message list that drives ``astep_stream()``.
    """
    return [
        {
            "role": "system",
            "content": (
                f"You received a {event.event_type!r} event from "
                f"{event.source!r} at {event.timestamp.isoformat()}. "
                f"Process this event and take appropriate action."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                event.payload, indent=2, default=str,
            ),
        },
    ]


# -- Rate limiter ----------------------------------------------------------


class TokenBucketRateLimiter:
    """Simple token-bucket rate limiter, stdlib only.

    ``rate`` is tokens per second. ``capacity`` is the burst size
    (defaults to ``max(rate, 1.0)``). Call ``acquire()`` before
    processing each event.
    """

    def __init__(self, rate: float, *, capacity: float | None = None) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(rate, 1.0)
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        if self.rate <= 0:
            return  # disabled
        async with self._lock:
            self._refill()
            while self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rate
                await asyncio.sleep(wait)
                self._refill()
            self._tokens -= 1.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now


# -- Factories -------------------------------------------------------------


def create_event_source(config: Any) -> EventSource:
    """Create an EventSource from a config object.

    Dispatches on ``config.type``. Returns ``NullEventSource`` for
    ``None`` or ``"null"`` types.
    """
    if config is None:
        from .sources.null import NullEventSource

        return NullEventSource()

    source_type = getattr(config, "type", None)
    if source_type is None or source_type == "null":
        from .sources.null import NullEventSource

        return NullEventSource()
    elif source_type == "webhook":
        from .sources.webhook import HttpWebhookSource

        source_id = config.source_id or f"event:{config.path}"
        return HttpWebhookSource(source_id, config=config)
    elif source_type == "cron":
        from .sources.cron import CronSource

        source_id = config.source_id or f"event:cron:{config.event_type}"
        return CronSource(source_id, config=config)
    elif source_type == "kafka":
        from .sources.kafka import KafkaSource

        source_id = config.source_id or f"event:kafka:{config.topic}:{config.consumer_group}"
        return KafkaSource(source_id, config=config)
    elif source_type == "redis":
        from .sources.redis import RedisStreamSource

        source_id = config.source_id or f"event:redis:{config.stream}:{config.consumer_group}"
        return RedisStreamSource(source_id, config=config)
    else:
        raise ValueError(f"Unknown event source type: {source_type!r}")


def create_event_sink(config: Any) -> EventSink:
    """Create an EventSink from a config object.

    Dispatches on ``config.type``. Returns ``NullSink`` for ``None``
    or ``"null"`` types.
    """
    if config is None:
        from .sinks.null import NullSink

        return NullSink()

    sink_type = getattr(config, "type", None)
    if sink_type is None or sink_type == "null":
        from .sinks.null import NullSink

        return NullSink()
    elif sink_type == "log":
        from .sinks.log import LogSink

        return LogSink(config=config)
    elif sink_type == "http_callback":
        from .sinks.http_callback import HttpCallbackSink

        return HttpCallbackSink(config=config)
    elif sink_type == "kafka":
        from .sinks.kafka import KafkaSink

        return KafkaSink(config=config)
    elif sink_type == "redis":
        from .sinks.redis import RedisStreamSink

        return RedisStreamSink(config=config)
    else:
        raise ValueError(f"Unknown event sink type: {sink_type!r}")
