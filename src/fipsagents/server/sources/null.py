"""Null event source -- never yields events, for testing."""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..events import EventSource, InboundEvent


class NullEventSource(EventSource):
    """Event source that never produces events."""

    def __init__(self) -> None:
        super().__init__("null")

    async def consume(self) -> AsyncIterator[InboundEvent]:
        return
        yield  # make this a generator
