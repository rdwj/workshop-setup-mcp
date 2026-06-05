"""HTTP webhook event source with HMAC-SHA256 verification."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..events import EventSource, InboundEvent, TokenBucketRateLimiter

logger = logging.getLogger(__name__)


class HttpWebhookSource(EventSource):
    """Registers a POST route on a FastAPI app and queues incoming events.

    HMAC-SHA256 verification is enabled when ``config.secret`` is set.
    The expected signature format is ``sha256=<hex>`` (GitHub convention).
    Verification uses :func:`hmac.compare_digest` for timing-attack safety.
    """

    def __init__(self, source_id: str, *, config: Any = None) -> None:
        super().__init__(source_id, config=config)
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._limiter = TokenBucketRateLimiter(
            getattr(config, "max_events_per_second", 10.0),
        )
        self._path: str = config.path
        self._secret: str | None = getattr(config, "secret", None)
        self._signature_header: str = getattr(
            config, "signature_header", "X-Hub-Signature-256",
        )
        self._event_type_header: str = getattr(
            config, "event_type_header", "X-GitHub-Event",
        )
        self._app: Any = None

    # -- HMAC verification ---------------------------------------------------

    def _verify_signature(self, body: bytes, header_value: str) -> bool:
        """Check ``sha256=<hex>`` signature against the shared secret."""
        expected = "sha256=" + hmac.new(
            self._secret.encode(),  # type: ignore[union-attr]
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(header_value, expected)

    # -- EventSource interface -----------------------------------------------

    async def setup(self, **kwargs: Any) -> None:
        """Register the webhook POST route on the FastAPI app.

        Raises :class:`ValueError` if ``app=`` is not provided.
        """
        app = kwargs.get("app")
        if app is None:
            raise ValueError("HttpWebhookSource requires app= in setup()")
        self._app = app

        source = self  # capture for the closure

        @app.post(source._path, status_code=202)
        async def _webhook_handler(request: Request) -> JSONResponse:
            body = await request.body()

            # HMAC verification when a secret is configured
            if source._secret:
                sig_header = request.headers.get(source._signature_header)
                if not sig_header:
                    return JSONResponse(
                        {"error": "Missing signature header"},
                        status_code=401,
                    )
                if not source._verify_signature(body, sig_header):
                    return JSONResponse(
                        {"error": "Invalid signature"},
                        status_code=401,
                    )

            # Rate limit before queuing
            await source._limiter.acquire()

            # Parse payload -- non-JSON bodies are wrapped so downstream
            # always receives a dict.
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                payload = {"raw": body.decode("utf-8", errors="replace")}

            event_type = request.headers.get(
                source._event_type_header, "webhook",
            )

            event = InboundEvent(
                event_id=uuid4().hex,
                event_type=event_type,
                payload=payload,
                source=source.source_id,
                timestamp=datetime.now(tz=UTC),
                session_key=f"event:{source._path}",
            )
            await source._queue.put(event)

            return JSONResponse({"status": "accepted"}, status_code=202)

    async def consume(self) -> AsyncIterator[InboundEvent]:
        """Yield events from the internal queue."""
        while True:
            event = await self._queue.get()
            yield event

    async def close(self) -> None:
        """No-op -- FastAPI route cleanup is not needed."""
