"""Null event sink -- discards all events."""

from __future__ import annotations

from ..events import EventSink, OutboundEvent


class NullSink(EventSink):
    """Event sink that discards all events."""

    async def emit(self, event: OutboundEvent) -> None:
        pass
