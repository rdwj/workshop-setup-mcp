"""Kafka event sink wrapping aiokafka producer."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..events import EventSink, OutboundEvent

logger = logging.getLogger(__name__)


class KafkaSink(EventSink):
    """Publishes outbound events to a Kafka topic.

    Producer is lazily initialized on the first ``emit()`` call.
    Errors are logged but not raised (non-blocking).

    Requires the ``[kafka]`` extra: ``pip install 'fipsagents[kafka]'``.
    """

    def __init__(self, *, config: Any = None) -> None:
        self._config = config
        self._producer: Any = None
        self._bootstrap = config.bootstrap_servers
        self._topic = config.topic
        self._security_protocol = getattr(config, "security_protocol", None)
        self._sasl_mechanism = getattr(config, "sasl_mechanism", None)
        self._sasl_username = getattr(config, "sasl_username", None)
        self._sasl_password = getattr(config, "sasl_password", None)

    async def _get_producer(self) -> Any:
        if self._producer is None:
            from aiokafka import AIOKafkaProducer

            producer_kwargs: dict[str, Any] = {
                "bootstrap_servers": self._bootstrap,
                "value_serializer": (
                    lambda v: json.dumps(v, default=str).encode()
                ),
            }
            if self._security_protocol:
                producer_kwargs["security_protocol"] = (
                    self._security_protocol
                )
            if self._sasl_mechanism:
                producer_kwargs["sasl_mechanism"] = self._sasl_mechanism
            if self._sasl_username:
                producer_kwargs["sasl_plain_username"] = self._sasl_username
            if self._sasl_password:
                producer_kwargs["sasl_plain_password"] = self._sasl_password

            self._producer = AIOKafkaProducer(**producer_kwargs)
            await self._producer.start()
            logger.info(
                "KafkaSink: started producer for topic=%s", self._topic,
            )
        return self._producer

    async def emit(self, event: OutboundEvent) -> None:
        try:
            producer = await self._get_producer()
            await producer.send_and_wait(
                self._topic,
                value=event.model_dump(),
            )
        except Exception:
            logger.warning(
                "KafkaSink: failed to emit event %s",
                event.correlation_id,
                exc_info=True,
            )

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
            logger.info("KafkaSink: stopped")
