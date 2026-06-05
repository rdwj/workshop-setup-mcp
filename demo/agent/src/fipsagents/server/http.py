"""HTTP-backed implementations of FeedbackStore / SessionStore / TraceStore.

Delegates persistence to a sibling ``fipsagents-platform`` service over
its REST surface (`/v1/sessions`, `/v1/traces`, `/v1/feedback`).  The
wire shape is the one documented on the platform service's OpenAPI;
the agent imports nothing from that repo, only speaks HTTP to it.

Authorization is forwarded per-request when the inbound chat request
carried an ``Authorization: Bearer <jwt>`` header (captured by the
server's auth-forwarding middleware into a contextvar).  When no
per-request token is present a static ``platform_token`` from
``StorageConfig`` is used — that's the housekeeping / service-to-service
fallback.

W3C Trace Context (``traceparent``) is forwarded verbatim from the
inbound request when present, so the platform's writes participate in
the same distributed trace as the chat completion that generated them.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime
from typing import Any

import httpx

from .feedback import (
    FeedbackRecord,
    FeedbackStats,
    FeedbackStore,
    _compute_window_end,
)
from .sessions import SessionStore
from .tracing import Span, Trace, TraceStore, TraceSummary, _summary_from_dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request context (set by auth-forwarding middleware in app.py)
# ---------------------------------------------------------------------------


_current_authorization: ContextVar[str | None] = ContextVar(
    "fipsagents_current_authorization", default=None,
)
_current_traceparent: ContextVar[str | None] = ContextVar(
    "fipsagents_current_traceparent", default=None,
)


def set_request_context(
    *, authorization: str | None, traceparent: str | None,
) -> tuple[Any, Any]:
    """Bind per-request authorization + traceparent to the current context.

    Returns reset tokens for the caller to pass to :func:`reset_request_context`.
    Used by ``OpenAIChatServer``'s middleware so outgoing ``Http*Store``
    calls forward the inbound JWT and trace context.
    """
    auth_tok = _current_authorization.set(authorization)
    tp_tok = _current_traceparent.set(traceparent)
    return auth_tok, tp_tok


def reset_request_context(tokens: tuple[Any, Any]) -> None:
    auth_tok, tp_tok = tokens
    _current_authorization.reset(auth_tok)
    _current_traceparent.reset(tp_tok)


# ---------------------------------------------------------------------------
# PlatformError + shared client
# ---------------------------------------------------------------------------


class PlatformError(RuntimeError):
    """Raised when the platform service returns an error or is unreachable.

    Wraps the upstream status code and response body when available so
    callers can distinguish transport failures from 4xx/5xx responses.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _PlatformClient:
    """Thin httpx wrapper shared by the three Http*Store classes.

    Centralises base-URL handling, bearer-token forwarding (per-request
    contextvar > static config token) and trace-context propagation.
    Each method maps a single HTTP call.  Connection re-use is provided
    by the underlying ``httpx.AsyncClient``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        static_token: str = "",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("_PlatformClient requires a base_url")
        # httpx prefixes path with the URL's path component, so strip a
        # trailing slash to keep concatenation predictable.
        self._base_url = base_url.rstrip("/")
        self._static_token = static_token
        # ``transport`` is a test seam: production code never sets it,
        # but tests inject ASGITransport (in-process platform app) or
        # MockTransport (canned wire-shape responses).
        kwargs: dict[str, Any] = {
            "base_url": self._base_url, "timeout": timeout,
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        token = _current_authorization.get() or (
            f"Bearer {self._static_token}" if self._static_token else None
        )
        if token:
            headers["Authorization"] = token
        traceparent = _current_traceparent.get()
        if traceparent:
            headers["traceparent"] = traceparent
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (200, 201),
        not_found_returns_none: bool = False,
    ) -> tuple[int, Any]:
        """Send a request.  Returns (status, parsed_body_or_None).

        - 2xx in ``ok_statuses`` returns the JSON body (or ``None`` for 204).
        - 404 returns (404, None) when ``not_found_returns_none`` is True
          so the caller can map it to ``None`` per the ABC.
        - Any other non-OK status raises :class:`PlatformError`.
        - Transport errors (connect refused, timeout) raise :class:`PlatformError`.
        """
        try:
            resp = await self._client.request(
                method, path, json=json, params=params, headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise PlatformError(
                f"platform unreachable: {exc.__class__.__name__}: {exc}",
            ) from exc

        if resp.status_code in ok_statuses:
            if resp.status_code == 204 or not resp.content:
                return resp.status_code, None
            return resp.status_code, resp.json()
        if resp.status_code == 404 and not_found_returns_none:
            return 404, None
        # Best-effort body extraction for the error message.
        try:
            body = resp.text
        except Exception:  # noqa: BLE001
            body = "<unreadable>"
        raise PlatformError(
            f"platform {resp.status_code}: {body[:500]}",
            status_code=resp.status_code,
        )


# ---------------------------------------------------------------------------
# HttpSessionStore
# ---------------------------------------------------------------------------


class HttpSessionStore(SessionStore):
    """SessionStore that delegates to ``fipsagents-platform``.

    Maps:

    - ``create``       → ``POST /v1/sessions``
    - ``load``         → ``GET /v1/sessions/{id}``
    - ``save``         → ``PUT /v1/sessions/{id}`` (upsert)
    - ``update``       → ``PATCH /v1/sessions/{id}``
    - ``exists``       → ``HEAD /v1/sessions/{id}``
    - ``delete``       → ``DELETE /v1/sessions/{id}``
    - ``delete_before`` → no platform endpoint; logged no-op (the
      platform owns its own housekeeping cycle).
    """

    def __init__(
        self,
        base_url: str,
        *,
        static_token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = _PlatformClient(
            base_url, static_token=static_token, transport=transport,
        )

    async def create(self, session_id: str | None = None) -> str:
        body: dict[str, Any] = {}
        if session_id is not None:
            body["session_id"] = session_id
        _, data = await self._client.request("POST", "/v1/sessions", json=body)
        return data["session_id"]

    async def load(self, session_id: str) -> list[dict] | None:
        status, data = await self._client.request(
            "GET", f"/v1/sessions/{session_id}", not_found_returns_none=True,
        )
        if status == 404:
            return None
        return data["messages"]

    async def save(self, session_id: str, messages: list[dict]) -> None:
        await self._client.request(
            "PUT",
            f"/v1/sessions/{session_id}",
            json={"messages": messages},
        )

    async def update(
        self,
        session_id: str,
        *,
        cost_data: dict | None = None,
    ) -> bool:
        if cost_data is None:
            return await self.exists(session_id)
        body: dict[str, Any] = {"cost_data": cost_data}
        status, _ = await self._client.request(
            "PATCH",
            f"/v1/sessions/{session_id}",
            json=body,
            not_found_returns_none=True,
        )
        return status != 404

    async def get_cost_data(self, session_id: str) -> dict:
        # Mirrors the SQLite/Postgres contract: empty dict when the
        # session is missing or has no cost_data yet. Requires
        # fipsagents-platform >= 0.2.1 (which exposes the GET endpoint);
        # against older platforms the route 404s on every call and we
        # degrade gracefully to last-write-wins semantics.
        status, data = await self._client.request(
            "GET",
            f"/v1/sessions/{session_id}/cost_data",
            not_found_returns_none=True,
        )
        if status == 404 or data is None:
            return {}
        return data.get("cost_data") or {}

    async def delete(self, session_id: str) -> bool:
        status, _ = await self._client.request(
            "DELETE",
            f"/v1/sessions/{session_id}",
            not_found_returns_none=True,
        )
        return status != 404

    async def exists(self, session_id: str) -> bool:
        # HEAD has no body and the platform returns 200 / 404.
        status, _ = await self._client.request(
            "HEAD",
            f"/v1/sessions/{session_id}",
            not_found_returns_none=True,
        )
        return status == 200

    async def delete_before(self, cutoff: datetime) -> int:
        logger.debug(
            "HttpSessionStore.delete_before is a no-op; "
            "platform owns housekeeping",
        )
        return 0

    async def close(self) -> None:
        await self._client.close()


# ---------------------------------------------------------------------------
# HttpTraceStore
# ---------------------------------------------------------------------------


class HttpTraceStore(TraceStore):
    """TraceStore that delegates to ``fipsagents-platform``.

    Maps:

    - ``save_trace``    → ``POST /v1/traces`` (upsert)
    - ``get_trace``     → ``GET /v1/traces/{id}``
    - ``list_traces``   → ``GET /v1/traces?limit&offset``
    - ``delete_before`` → no platform endpoint; logged no-op.
    """

    def __init__(
        self,
        base_url: str,
        *,
        static_token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = _PlatformClient(
            base_url, static_token=static_token, transport=transport,
        )

    async def save_trace(self, trace: Trace) -> None:
        # Use asdict so Span objects flatten correctly.  The platform's
        # TraceIn schema is a structural superset of the dataclass.
        await self._client.request("POST", "/v1/traces", json=asdict(trace))

    async def get_trace(self, trace_id: str) -> Trace | None:
        status, data = await self._client.request(
            "GET",
            f"/v1/traces/{trace_id}",
            not_found_returns_none=True,
        )
        if status == 404:
            return None
        spans = [
            Span(
                trace_id=s["trace_id"],
                span_id=s["span_id"],
                parent_span_id=s.get("parent_span_id"),
                name=s.get("name", ""),
                start_time=s.get("start_time", 0.0),
                end_time=s.get("end_time"),
                status=s.get("status", "ok"),
                attributes=s.get("attributes", {}),
                events=s.get("events", []),
            )
            for s in data.get("spans", [])
        ]
        return Trace(
            trace_id=data["trace_id"],
            started_at=data["started_at"],
            ended_at=data.get("ended_at"),
            model=data.get("model"),
            session_id=data.get("session_id"),
            status=data.get("status", "ok"),
            spans=spans,
        )

    async def list_traces(
        self, *, limit: int = 50, offset: int = 0,
    ) -> list[TraceSummary]:
        _, data = await self._client.request(
            "GET",
            "/v1/traces",
            params={"limit": limit, "offset": offset},
        )
        return [_summary_from_dict(d) for d in data]

    async def delete_before(self, cutoff: datetime) -> int:
        logger.debug(
            "HttpTraceStore.delete_before is a no-op; "
            "platform owns housekeeping",
        )
        return 0

    async def close(self) -> None:
        await self._client.close()


# ---------------------------------------------------------------------------
# HttpFeedbackStore
# ---------------------------------------------------------------------------


def _record_from_dict(d: dict[str, Any]) -> FeedbackRecord:
    return FeedbackRecord(
        feedback_id=d["feedback_id"],
        trace_id=d["trace_id"],
        session_id=d.get("session_id"),
        rating=d["rating"],
        comment=d.get("comment"),
        correction=d.get("correction"),
        model_id=d.get("model_id"),
        latency_ms=d.get("latency_ms"),
        turn_index=d.get("turn_index"),
        agent_type=d.get("agent_type"),
        created_at=d["created_at"],
        user_id=d.get("user_id", "anonymous"),
    )


class HttpFeedbackStore(FeedbackStore):
    """FeedbackStore that delegates to ``fipsagents-platform``.

    Maps:

    - ``add``           → ``POST /v1/feedback``
    - ``get``           → ``GET /v1/feedback/{id}``
    - ``query``         → ``GET /v1/feedback`` (filter via query params)
    - ``stats``         → ``GET /v1/feedback/stats``
    - ``update``        → ``PATCH /v1/feedback/{id}``
    - ``delete_before`` → no platform endpoint; logged no-op.

    Note on identity: the platform's ``POST /v1/feedback`` always
    generates a fresh ``feedback_id`` and ``created_at`` server-side.
    Any values on the inbound :class:`FeedbackRecord` are discarded and
    replaced with the platform's authoritative copies.  Likewise
    ``user_id`` is set from the gateway-issued bearer subject by the
    platform — the agent's value is informational only.
    """

    def __init__(
        self,
        base_url: str,
        *,
        static_token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = _PlatformClient(
            base_url, static_token=static_token, transport=transport,
        )

    async def add(self, record: FeedbackRecord) -> str:
        body = {
            "rating": record.rating,
            "trace_id": record.trace_id,
            "session_id": record.session_id,
            "comment": record.comment,
            "correction": record.correction,
            "model_id": record.model_id,
            "latency_ms": record.latency_ms,
            "turn_index": record.turn_index,
            "agent_type": record.agent_type,
        }
        _, data = await self._client.request(
            "POST",
            "/v1/feedback",
            json={k: v for k, v in body.items() if v is not None},
        )
        return data["feedback_id"]

    async def get(self, feedback_id: str) -> FeedbackRecord | None:
        status, data = await self._client.request(
            "GET",
            f"/v1/feedback/{feedback_id}",
            not_found_returns_none=True,
        )
        if status == 404:
            return None
        return _record_from_dict(data)

    async def query(
        self,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[FeedbackRecord]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if trace_id is not None:
            params["trace_id"] = trace_id
        if session_id is not None:
            params["session_id"] = session_id
        if user_id is not None:
            params["user_id"] = user_id
        if since is not None:
            params["since"] = since.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        _, data = await self._client.request(
            "GET", "/v1/feedback", params=params,
        )
        return [_record_from_dict(d) for d in data]

    async def stats(
        self,
        *,
        window: str = "day",
        agent_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[FeedbackStats]:
        params: dict[str, Any] = {"window": window}
        if agent_type is not None:
            params["agent_type"] = agent_type
        if since is not None:
            params["since"] = since.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        _, data = await self._client.request(
            "GET", "/v1/feedback/stats", params=params,
        )
        return [
            FeedbackStats(
                window_start=row["window_start"],
                # Platform may return window_end already computed; if
                # absent, derive it locally so the caller sees the same
                # shape as the SQLite/Postgres backends.
                window_end=row.get(
                    "window_end",
                    _compute_window_end(row["window_start"], window),
                ),
                agent_type=row.get("agent_type"),
                thumbs_up=row["thumbs_up"],
                thumbs_down=row["thumbs_down"],
                total=row["total"],
            )
            for row in data
        ]

    async def update(
        self,
        feedback_id: str,
        *,
        rating: int | None = None,
        comment: str | None = None,
        correction: str | None = None,
    ) -> FeedbackRecord | None:
        body: dict[str, Any] = {}
        if rating is not None:
            body["rating"] = rating
        if comment is not None:
            body["comment"] = comment
        if correction is not None:
            body["correction"] = correction
        if not body:
            return await self.get(feedback_id)
        status, data = await self._client.request(
            "PATCH",
            f"/v1/feedback/{feedback_id}",
            json=body,
            not_found_returns_none=True,
        )
        if status == 404:
            return None
        return _record_from_dict(data)

    async def delete_before(self, cutoff: datetime) -> int:
        logger.debug(
            "HttpFeedbackStore.delete_before is a no-op; "
            "platform owns housekeeping",
        )
        return 0

    async def close(self) -> None:
        await self._client.close()
