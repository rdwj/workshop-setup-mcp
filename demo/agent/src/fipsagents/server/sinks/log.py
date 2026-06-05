"""Structured JSON logging event sink."""

from __future__ import annotations

import logging
from typing import Any

from ..events import EventSink, OutboundEvent

logger = logging.getLogger("fipsagents.server.events.sink")


class LogSink(EventSink):
    """Event sink that logs events as structured JSON."""

    def __init__(self, *, config: Any = None) -> None:
        level_name = getattr(config, "level", "INFO") if config else "INFO"
        self._level = getattr(logging, level_name.upper(), logging.INFO)

    async def emit(self, event: OutboundEvent) -> None:
        logger.log(
            self._level,
            "Event sink: %s",
            event.model_dump_json(),
        )
