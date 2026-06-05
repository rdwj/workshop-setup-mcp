"""Redis Streams event sink."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..events import EventSink, OutboundEvent

logger = logging.getLogger(__name__)


class RedisStreamSink(EventSink):
    """Publishes outbound events to a Redis Stream via XADD.

    Client is lazily initialized on the first ``emit()`` call.
    Errors are logged but not raised (non-blocking).

    Requires the ``[redis]`` extra: ``pip install 'fipsagents[redis]'``.
    """

    def __init__(self, *, config: Any = None) -> None:
        self._config = config
        self._client: Any = None
        self._url = config.url
        self._stream = config.stream
        self._maxlen = getattr(config, "maxlen", None)

    async def _get_client(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self._url, decode_responses=True,
            )
            logger.info(
                "RedisStreamSink: connected to stream=%s", self._stream,
            )
        return self._client

    async def emit(self, event: OutboundEvent) -> None:
        try:
            client = await self._get_client()
            fields = {
                "correlation_id": event.correlation_id,
                "event_type": event.event_type,
                "payload": json.dumps(event.payload, default=str),
                "source": event.source,
                "timestamp": event.timestamp.isoformat(),
            }
            kwargs: dict[str, Any] = {}
            if self._maxlen is not None:
                kwargs["maxlen"] = self._maxlen
                kwargs["approximate"] = True
            await client.xadd(self._stream, fields, **kwargs)
        except Exception:
            logger.warning(
                "RedisStreamSink: failed to emit event %s",
                event.correlation_id,
                exc_info=True,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("RedisStreamSink: stopped")
