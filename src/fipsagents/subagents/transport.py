"""Subagent invocation transports.

Two concrete implementations:

``RemoteSubagentTransport``
    Posts to a remote agent's ``/v1/chat/completions`` endpoint via
    ``httpx``.  Non-streaming (``stream=False``).  Injects W3C Trace Context
    headers when a ``traceparent`` is supplied by the caller.

``InProcessSubagentTransport``
    Instantiates a :class:`~fipsagents.baseagent.BaseAgent` subclass in the
    same process, calls ``setup()`` once, and drives one turn through
    ``astep_stream``.  The subagent instance is cached across invocations so
    setup cost is paid only once per transport lifetime.

Both implementations translate all exceptions into :class:`SubagentError`
subclasses so the parent's dispatch loop can branch on failure type without
catching raw network or Python exceptions.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import httpx

from fipsagents.subagents.types import (
    SubagentCrashedError,
    SubagentRemoteError,
    SubagentResult,
    SubagentTimeoutError,
)

if TYPE_CHECKING:
    from fipsagents.baseagent.config import InProcessTransportConfig, RemoteTransportConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class SubagentTransport(ABC):
    """Abstract base for subagent invocation transports.

    Each concrete implementation is responsible for invoking the subagent
    and returning a :class:`~fipsagents.subagents.types.SubagentResult`.
    All exceptions must be converted to subclasses of
    :class:`~fipsagents.subagents.types.SubagentError` so the parent's loop
    can dispatch on failure type.
    """

    @abstractmethod
    async def invoke(
        self,
        *,
        task: str,
        context: str = "",
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 60.0,
    ) -> SubagentResult:
        """Invoke the subagent with *task* and return its result.

        Parameters
        ----------
        task:
            The main instruction or question to pass to the subagent.
        context:
            Optional additional context to prepend before ``task``.  When
            non-empty it is placed on its own line before the task text.
        headers:
            Extra HTTP headers to include in the outgoing request.  The
            remote transport merges these with any trace headers it adds.
            Ignored by the inprocess transport (no HTTP boundary).
        timeout_seconds:
            Per-call wall-time cap.  On expiry raises
            :class:`~fipsagents.subagents.types.SubagentTimeoutError`.

        Returns
        -------
        SubagentResult
            Typed outcome of the subagent invocation.

        Raises
        ------
        SubagentTimeoutError
            The call did not complete within *timeout_seconds*.
        SubagentRemoteError
            The remote endpoint returned a non-success HTTP status or the
            connection broke (remote transport only).
        SubagentCrashedError
            An unhandled exception escaped the inprocess subagent
            (inprocess transport only).
        """


# ---------------------------------------------------------------------------
# Remote transport
# ---------------------------------------------------------------------------


def _build_completions_url(base_url: str) -> str:
    """Normalize *base_url* and append the chat completions path.

    Handles three common forms:
    - ``http://host:8080/v1/chat/completions`` → ``http://host:8080/v1/chat/completions``
    - ``http://host:8080/v1``                  → ``http://host:8080/v1/chat/completions``
    - ``http://host:8080``                     → ``http://host:8080/v1/chat/completions``
    """
    url = base_url.rstrip("/")
    if url.endswith("/v1/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def _build_user_message(task: str, context: str) -> str:
    """Combine context and task into a single user message body."""
    if context:
        return f"{context}\n{task}"
    return task


class RemoteSubagentTransport(SubagentTransport):
    """Invokes a subagent over HTTP via its OpenAI-compatible
    ``/v1/chat/completions`` endpoint.

    Wire shape: standard OpenAI chat-completions request with
    ``stream=False``.  The response is parsed as JSON; the assistant
    content is extracted from ``choices[0].message.content`` and token
    usage from the ``usage`` block.

    Trace context is injected via the ``headers`` argument supplied by the
    caller (typically from ``inject_trace_context()`` in Step 5).  For v1
    this transport does not create its own OTEL spans — that is Step 6.

    Cost (``cost_usd``) is always ``0.0`` in v1.  The design doc scopes
    cost roll-up to a later step that wires through the session store; the
    remote endpoint owns its own pricing and v1 does not collect it on the
    parent side.

    Parameters
    ----------
    agent_name:
        Logical name of the subagent (used in error messages).
    config:
        :class:`~fipsagents.baseagent.config.RemoteTransportConfig` carrying
        the target URL and default timeout.
    http_client:
        Optional pre-constructed ``httpx.AsyncClient``.  Intended for
        tests (``MockTransport``-backed clients).  When provided the
        transport does **not** close it on completion.  Production code
        leaves this as ``None`` and the transport constructs and closes its
        own client on each call.
    """

    def __init__(
        self,
        agent_name: str,
        config: RemoteTransportConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._injected_client = http_client
        self._completions_url = _build_completions_url(config.url)

    async def invoke(
        self,
        *,
        task: str,
        context: str = "",
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 60.0,
    ) -> SubagentResult:
        """POST a single user turn to the remote agent and parse the result.

        The ``model`` field on the request body is set to ``"subagent"`` as
        an informational placeholder — the receiving agent uses its own
        configured model.  The ``OpenAIChatServer`` accepts any non-empty
        string for ``model`` and ignores it when the agent has its own config.

        Raises
        ------
        SubagentTimeoutError
            The HTTP call did not complete within *timeout_seconds*.
        SubagentRemoteError
            The server returned a non-2xx status, or the connection failed.
        """
        message_content = _build_user_message(task, context)
        body = {
            "model": "subagent",
            "messages": [{"role": "user", "content": message_content}],
            "stream": False,
        }

        # Merge caller-supplied headers (trace context, auth, etc.).
        merged_headers: dict[str, str] = {}
        if headers:
            merged_headers.update(headers)

        client_owned = self._injected_client is None
        client: httpx.AsyncClient = (
            self._injected_client
            if self._injected_client is not None
            else httpx.AsyncClient(timeout=timeout_seconds)
        )

        try:
            coro = client.post(
                self._completions_url,
                json=body,
                headers=merged_headers,
            )
            try:
                resp = await asyncio.wait_for(coro, timeout=timeout_seconds)
            except asyncio.TimeoutError:
                raise SubagentTimeoutError(self._agent_name, timeout_seconds)

            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    detail = resp.text
                if isinstance(detail, dict):
                    detail = str(detail)
                raise SubagentRemoteError(
                    self._agent_name,
                    status_code=resp.status_code,
                    detail=detail,
                )

            try:
                data = resp.json()
            except Exception as exc:
                raise SubagentRemoteError(
                    self._agent_name,
                    status_code=resp.status_code,
                    detail=f"response was not valid JSON: {exc}",
                ) from exc

            return self._parse_response(data)

        except (SubagentTimeoutError, SubagentRemoteError):
            raise
        except httpx.TimeoutException:
            raise SubagentTimeoutError(self._agent_name, timeout_seconds)
        except httpx.RequestError as exc:
            raise SubagentRemoteError(
                self._agent_name,
                status_code=None,
                detail=str(exc),
            ) from exc
        finally:
            if client_owned:
                await client.aclose()

    def _parse_response(self, data: dict) -> SubagentResult:
        """Extract content, usage, and finish_reason from a JSON response body."""
        choices = data.get("choices") or []
        first_choice = choices[0] if choices else {}
        message = first_choice.get("message") or {}
        content = message.get("content") or ""
        finish_reason = first_choice.get("finish_reason") or "stop"

        tool_calls = message.get("tool_calls") or []
        tool_calls_made = len(tool_calls)

        usage = data.get("usage") or {}
        tokens_used: dict[str, int] = {
            "input": int(usage.get("prompt_tokens") or 0),
            "output": int(usage.get("completion_tokens") or 0),
            "cached": int(usage.get("cached_tokens") or 0),
        }

        return SubagentResult(
            agent_name=self._agent_name,
            content=content,
            tokens_used=tokens_used,
            tool_calls_made=tool_calls_made,
            cost_usd=0.0,  # v1: remote endpoint owns pricing; cost roll-up is Step 6+
            span_id=None,  # populated by Step 5/6 when tracing is enabled
            finish_reason=finish_reason,
        )


# ---------------------------------------------------------------------------
# Inprocess transport
# ---------------------------------------------------------------------------


class InProcessSubagentTransport(SubagentTransport):
    """Invokes a subagent that lives in the same Python process.

    The class named by ``config.class_path`` is imported with
    ``importlib``, instantiated, and set up on the first ``invoke`` call.
    Subsequent calls reuse the cached instance so ``setup()`` runs only
    once per transport lifetime.

    Conversation isolation note
    ---------------------------
    The cached agent instance retains its ``messages`` list across
    invocations within a single transport instance.  This is intentional
    for agents that benefit from continuity (multi-turn sub-conversations).
    When full isolation per call is required, construct a new
    ``InProcessSubagentTransport`` for each invocation.

    Trace headers note
    ------------------
    The ``headers`` argument is accepted for API uniformity but silently
    ignored — there is no HTTP boundary at which to inject trace context.
    Inprocess spans are emitted by the parent's trace collector via
    ``SubagentInvoked`` / ``SubagentCompleted`` events (Step 5).

    Tool-call counting
    ------------------
    ``tool_calls_made`` is derived from the count of
    :class:`~fipsagents.baseagent.events.ToolResultEvent` events emitted
    during the subagent's step.  Each ``ToolResultEvent`` corresponds to one
    completed tool execution; this is more reliable than counting
    ``ToolCallDelta`` index values because the model may stream multiple
    deltas for a single call.

    Parameters
    ----------
    agent_name:
        Logical name of the subagent (used in error messages and the
        result's ``agent_name`` field).
    config:
        :class:`~fipsagents.baseagent.config.InProcessTransportConfig`
        carrying the dotted class path and optional config file path.
    """

    def __init__(
        self,
        agent_name: str,
        config: InProcessTransportConfig,
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._agent: object | None = None  # BaseAgent subclass instance, lazily created

    async def _get_agent(self) -> object:
        """Lazily instantiate and set up the subagent; cache for reuse."""
        if self._agent is not None:
            return self._agent

        # Import the class.
        try:
            module_path, class_name = self._config.class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            agent_class = getattr(module, class_name)
        except Exception as exc:
            raise SubagentCrashedError(
                self._agent_name,
                original=exc,
            ) from exc

        # Instantiate.  BaseAgent accepts config_path as the first positional arg.
        try:
            if self._config.config_path is not None:
                agent = agent_class(self._config.config_path)
            else:
                agent = agent_class()
            await agent.setup()
        except Exception as exc:
            raise SubagentCrashedError(
                self._agent_name,
                original=exc,
            ) from exc

        self._agent = agent
        return agent

    async def invoke(
        self,
        *,
        task: str,
        context: str = "",
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 60.0,
    ) -> SubagentResult:
        """Run one agent turn and collect the result.

        Parameters
        ----------
        task:
            Instruction passed as a user-role message to the subagent.
        context:
            Optional context prepended before *task* on its own line.
        headers:
            Ignored — no HTTP boundary in inprocess transport.
        timeout_seconds:
            Wall-time cap around the entire ``astep_stream`` consumption.
            On expiry raises :class:`SubagentTimeoutError`.

        Raises
        ------
        SubagentTimeoutError
            The subagent stream did not complete within *timeout_seconds*.
        SubagentCrashedError
            An unhandled exception escaped the subagent's stream loop.
        """
        from fipsagents.baseagent.events import (
            ContentDelta,
            StreamComplete,
            ToolResultEvent,
        )

        try:
            agent = await self._get_agent()
        except SubagentCrashedError:
            raise

        # Propagate delegation depth so the child can enforce its own
        # max_depth cap.  The parent's _delegate() writes the depth into
        # the x-subagent-depth header; we read it here and temporarily
        # set it on the child agent for the duration of this call.
        child_depth = 0
        if headers:
            raw_depth = headers.get("x-subagent-depth")
            if raw_depth is not None:
                try:
                    child_depth = int(raw_depth)
                except ValueError:
                    pass
        prev_depth: int = getattr(agent, "_delegation_depth", 0)
        try:
            agent._delegation_depth = child_depth  # type: ignore[attr-defined]
        except AttributeError:
            pass  # stub agents that don't expose the attribute

        message_content = _build_user_message(task, context)
        agent.messages.append({"role": "user", "content": message_content})  # type: ignore[attr-defined]

        async def _consume_stream() -> SubagentResult:
            content_parts: list[str] = []
            tool_calls_made = 0
            finish_reason = "stop"
            tokens_used: dict[str, int] = {"input": 0, "output": 0, "cached": 0}

            async for event in agent.astep_stream():  # type: ignore[attr-defined]
                if isinstance(event, ContentDelta):
                    content_parts.append(event.content)
                elif isinstance(event, ToolResultEvent):
                    tool_calls_made += 1
                elif isinstance(event, StreamComplete):
                    finish_reason = event.finish_reason
                    m = event.metrics
                    tokens_used = {
                        "input": int(m.prompt_tokens or 0),
                        "output": int(m.completion_tokens or 0),
                        "cached": 0,
                    }

            return SubagentResult(
                agent_name=self._agent_name,
                content="".join(content_parts),
                tokens_used=tokens_used,
                tool_calls_made=tool_calls_made,
                cost_usd=0.0,
                span_id=None,
                finish_reason=finish_reason,
            )

        try:
            result = await asyncio.wait_for(
                _consume_stream(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            raise SubagentTimeoutError(self._agent_name, timeout_seconds)
        except (SubagentCrashedError, SubagentTimeoutError):
            raise
        except Exception as exc:
            raise SubagentCrashedError(
                self._agent_name,
                original=exc,
            ) from exc
        finally:
            # Restore the agent's previous depth so re-use across multiple
            # parent calls doesn't carry stale depth state.
            try:
                agent._delegation_depth = prev_depth  # type: ignore[attr-defined]
            except AttributeError:
                pass

        return result
