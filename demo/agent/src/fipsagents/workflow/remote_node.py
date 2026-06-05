"""Remote workflow node — calls an already-deployed agent via HTTP."""

from __future__ import annotations

import asyncio
import logging
from typing import TypeVar

import httpx
from pydantic import ValidationError

from fipsagents.workflow.node import BaseNode
from fipsagents.baseagent.config import BackoffConfig

T = TypeVar("T")

logger = logging.getLogger(__name__)


class RemoteNodeError(Exception):
    """Raised when a remote node call fails after all retries."""


class RemoteNode(BaseNode):
    """Workflow node that delegates processing to a remote agent via HTTP POST.

    The remote agent exposes a single endpoint:

        POST {endpoint}{path}
        Content-Type: application/json

        {"state": { ... }, "state_type": "fully.qualified.ClassName"}

        Response 200: {"state": { ... updated state ... }}

    RemoteNode handles serialization (Pydantic model_dump/model_validate),
    retries with exponential backoff, and error mapping.

    HTTP-level retries are handled internally here. If all attempts fail,
    ``RemoteNodeError`` is raised and the WorkflowRunner's node-retry/error-edge
    mechanism takes over — the two layers are intentionally distinct.
    """

    def __init__(
        self,
        name: str,
        *,
        endpoint: str,
        path: str = "/process",
        timeout: float = 30.0,
        retries: int = 2,
        backoff: BackoffConfig | None = None,
    ):
        super().__init__(name=name)
        self.endpoint = endpoint.rstrip("/")
        self.path = path
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff or BackoffConfig()
        self._trace_headers: dict[str, str] = {}

    def set_trace_context(self, trace_id: str, span_id: str) -> None:
        """Set W3C Trace Context headers for outgoing requests.

        When set, all HTTP calls include a ``traceparent`` header
        linking downstream traces to the caller's trace.
        """
        try:
            from fipsagents.server.propagation import inject_trace_context
            self._trace_headers = inject_trace_context(trace_id, span_id)
        except ImportError:
            pass  # [otel] extra not installed

    async def process(self, state: T) -> T:
        url = f"{self.endpoint}{self.path}"
        state_type = type(state)
        payload = {
            "state": state.model_dump(),
            "state_type": f"{state_type.__module__}.{state_type.__qualname__}",
        }

        last_error = None
        for attempt in range(self.retries + 1):
            if attempt > 0:
                delay = min(
                    self.backoff.initial * (self.backoff.multiplier ** (attempt - 1)),
                    self.backoff.max,
                )
                logger.warning(
                    "Remote node '%s' retry %d/%d after %.1fs",
                    self.name, attempt, self.retries, delay,
                )
                await asyncio.sleep(delay)

            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        url, json=payload, headers=self._trace_headers,
                    )
                    resp.raise_for_status()
                    try:
                        data = resp.json()
                        return state_type.model_validate(data["state"])
                    except (KeyError, ValueError, ValidationError) as exc:
                        raise RemoteNodeError(
                            f"Remote node '{self.name}' received invalid response "
                            f"from {url}: {exc}"
                        ) from exc
            except httpx.HTTPStatusError as exc:
                last_error = exc
                logger.warning(
                    "Remote node '%s' HTTP %d from %s",
                    self.name, exc.response.status_code, url,
                )
            except httpx.RequestError as exc:
                last_error = exc
                logger.warning(
                    "Remote node '%s' request error: %s", self.name, exc,
                )

        raise RemoteNodeError(
            f"Remote node '{self.name}' failed after {self.retries + 1} "
            f"attempts calling {url}: {last_error}"
        ) from last_error
