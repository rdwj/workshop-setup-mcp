"""Kafka event source wrapping aiokafka consumer."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ..events import EventSource, InboundEvent, TokenBucketRateLimiter

logger = logging.getLogger(__name__)


class KafkaSource(EventSource):
    """Consumes messages from a Kafka topic via aiokafka.

    Offset commits are manual via ``acknowledge()`` -- auto-commit is
    disabled so the server controls exactly-once processing semantics.
    Consumer group membership handles load balancing across replicas.

    Requires the ``[kafka]`` extra: ``pip install 'fipsagents[kafka]'``.
    """

    def __init__(self, source_id: str, *, config: Any = None) -> None:
        super().__init__(source_id, config=config)
        self._consumer: Any = None
        self._limiter = TokenBucketRateLimiter(
            getattr(config, "max_events_per_second", 10.0),
        )
        self._bootstrap = config.bootstrap_servers
        self._topic = config.topic
        self._group = config.consumer_group
        self._auto_offset_reset = getattr(config, "auto_offset_reset", "latest")
        self._security_protocol = getattr(config, "security_protocol", None)
        self._sasl_mechanism = getattr(config, "sasl_mechanism", None)
        self._sasl_username = getattr(config, "sasl_username", None)
        self._sasl_password = getattr(config, "sasl_password", None)
        self._pending_offsets: dict[str, tuple[str, int]] = {}

    async def setup(self, **kwargs: Any) -> None:
        from aiokafka import AIOKafkaConsumer

        consumer_kwargs: dict[str, Any] = {
            "bootstrap_servers": self._bootstrap,
            "group_id": self._group,
            "auto_offset_reset": self._auto_offset_reset,
            "enable_auto_commit": False,
            "value_deserializer": lambda v: v,
        }
        if self._security_protocol:
            consumer_kwargs["security_protocol"] = self._security_protocol
        if self._sasl_mechanism:
            consumer_kwargs["sasl_mechanism"] = self._sasl_mechanism
        if self._sasl_username:
            consumer_kwargs["sasl_plain_username"] = self._sasl_username
        if self._sasl_password:
            consumer_kwargs["sasl_plain_password"] = self._sasl_password

        self._consumer = AIOKafkaConsumer(self._topic, **consumer_kwargs)
        await self._consumer.start()
        logger.info(
            "KafkaSource %s: consuming topic=%s group=%s",
            self.source_id, self._topic, self._group,
        )

    async def consume(self) -> AsyncIterator[InboundEvent]:
        assert self._consumer is not None
        async for msg in self._consumer:
            await self._limiter.acquire()
            try:
                payload = json.loads(msg.value)
            except (json.JSONDecodeError, ValueError):
                payload = {"raw": msg.value.decode("utf-8", errors="replace")}

            event_id = uuid4().hex
            event = InboundEvent(
                event_id=event_id,
                event_type=f"kafka.{self._topic}",
                payload=payload,
                source=self.source_id,
                timestamp=datetime.now(tz=UTC),
                metadata={
                    "partition": msg.partition,
                    "offset": msg.offset,
                    "key": msg.key.decode("utf-8") if msg.key else None,
                },
                session_key=f"event:kafka:{self._topic}:{self._group}",
            )
            self._pending_offsets[event_id] = (msg.topic, msg.offset)
            yield event

    async def acknowledge(self, event_id: str) -> None:
        if self._consumer is not None:
            self._pending_offsets.pop(event_id, None)
            await self._consumer.commit()

    async def close(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
            logger.info("KafkaSource %s: stopped", self.source_id)
