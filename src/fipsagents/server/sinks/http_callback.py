"""HTTP callback event sink -- POSTs events to a configured URL."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..events import EventSink, OutboundEvent

logger = logging.getLogger("fipsagents.server.events.sink")


class HttpCallbackSink(EventSink):
    """Event sink that POSTs events to a callback URL."""

    def __init__(self, *, config: Any = None) -> None:
        if config is None:
            raise ValueError("HttpCallbackSink requires config with url")
        self._url: str = config.url
        self._timeout: float = getattr(config, "timeout_seconds", 30.0)
        self._client: httpx.AsyncClient | None = None

    async def setup(self) -> None:
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def emit(self, event: OutboundEvent) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        try:
            await self._client.post(
                self._url,
                json=event.model_dump(mode="json"),
                headers={"Content-Type": "application/json"},
            )
        except Exception:
            logger.exception(
                "Failed to emit event %s to %s",
                event.correlation_id,
                self._url,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
