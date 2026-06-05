"""Redis Streams event source wrapping redis.asyncio."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from ..events import EventSource, InboundEvent, TokenBucketRateLimiter

logger = logging.getLogger(__name__)


class RedisStreamSource(EventSource):
    """Consumes messages from a Redis Stream via XREADGROUP.

    Consumer group membership and pending entry lists (XPENDING) provide
    load balancing and retry visibility. ``acknowledge()`` calls XACK.

    Requires the ``[redis]`` extra: ``pip install 'fipsagents[redis]'``.
    """

    def __init__(self, source_id: str, *, config: Any = None) -> None:
        super().__init__(source_id, config=config)
        self._client: Any = None
        self._limiter = TokenBucketRateLimiter(
            getattr(config, "max_events_per_second", 10.0),
        )
        self._url = config.url
        self._stream = config.stream
        self._group = config.consumer_group
        self._consumer_name = getattr(config, "consumer_name", "worker-0")
        self._block_ms = getattr(config, "block_ms", 5000)

    async def setup(self, **kwargs: Any) -> None:
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(self._url, decode_responses=True)
        try:
            await self._client.xgroup_create(
                self._stream, self._group, id="0", mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                pass  # group already exists
            else:
                raise
        logger.info(
            "RedisStreamSource %s: consuming stream=%s group=%s consumer=%s",
            self.source_id, self._stream, self._group, self._consumer_name,
        )

    async def consume(self) -> AsyncIterator[InboundEvent]:
        assert self._client is not None
        while True:
            entries = await self._client.xreadgroup(
                groupname=self._group,
                consumername=self._consumer_name,
                streams={self._stream: ">"},
                count=1,
                block=self._block_ms,
            )
            if not entries:
                continue

            for _stream_name, messages in entries:
                for msg_id, fields in messages:
                    await self._limiter.acquire()
                    try:
                        if "payload" in fields:
                            payload = json.loads(fields["payload"])
                        else:
                            payload = dict(fields)
                    except (json.JSONDecodeError, ValueError):
                        payload = dict(fields)

                    event_type = fields.get(
                        "event_type", f"redis.{self._stream}",
                    )

                    event = InboundEvent(
                        event_id=msg_id,
                        event_type=event_type,
                        payload=payload,
                        source=self.source_id,
                        timestamp=datetime.now(tz=UTC),
                        metadata={
                            "stream": self._stream,
                            "msg_id": msg_id,
                        },
                        session_key=(
                            f"event:redis:{self._stream}:{self._group}"
                        ),
                    )
                    yield event

    async def acknowledge(self, event_id: str) -> None:
        if self._client is not None:
            await self._client.xack(self._stream, self._group, event_id)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("RedisStreamSource %s: stopped", self.source_id)
