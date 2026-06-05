"""LLM client for BaseAgent — async wrappers around the OpenAI SDK.

Two endpoint families are supported:

- **Chat completions** (``/v1/chat/completions``) — the default. Provides
  ``call_model``, ``call_model_json``, ``call_model_stream``, and
  ``call_model_validated``.
- **Responses** (``/v1/responses``) — opt-in via ``platform.enabled`` in
  ``agent.yaml``. Provides ``call_model_responses``,
  ``call_model_responses_stream``, and ``moderate``. Delegates MCP
  orchestration, shield enforcement, and (optionally) moderation
  classification to OGX (LlamaStack rebrand) server-side.

All methods are async.  All LLM communication goes through the OpenAI SDK.
Any OpenAI-compatible endpoint (vLLM, LlamaStack, llm-d) works out of the box.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
from typing import Any, AsyncIterator, Callable, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from fipsagents.baseagent.config import LLMConfig, PlatformConfig, PlatformMcpServer
from fipsagents.baseagent.events import (
    ContentDelta,
    GuardrailFiredEvent,
    StreamComplete,
    StreamEvent,
    StreamMetrics,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Raised when an LLM call fails.

    Wraps the underlying provider exception so callers only need
    to catch one type.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_to_response_format(
    schema: type[BaseModel] | dict[str, Any],
) -> dict[str, Any]:
    """Convert a Pydantic model or raw JSON schema dict into the
    ``response_format`` value expected by the OpenAI API for structured output.
    """
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        json_schema = schema.model_json_schema()
        name = schema.__name__
    elif isinstance(schema, dict):
        json_schema = schema
        name = schema.get("title", "response")
    else:
        raise LLMError(
            f"schema must be a Pydantic model class or a JSON-schema dict, "
            f"got {type(schema).__name__}"
        )
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": json_schema,
        },
    }


def _parse_json_response(
    content: str,
    schema: type[BaseModel] | dict[str, Any],
) -> BaseModel | dict[str, Any]:
    """Parse a JSON string into the target type.

    Returns a Pydantic model instance when *schema* is a model class,
    otherwise a plain dict.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model returned invalid JSON: {exc}") from exc

    if isinstance(schema, type) and issubclass(schema, BaseModel):
        try:
            return schema.model_validate(data)
        except Exception as exc:
            raise LLMError(
                f"Model output failed schema validation: {exc}"
            ) from exc

    return data


# ---------------------------------------------------------------------------
# Response wrapper
# ---------------------------------------------------------------------------


class ModelResponse:
    """Thin wrapper around an OpenAI chat completion response for convenient access.

    Attributes
    ----------
    content:
        The text content of the first choice, or ``None`` if the model
        returned only tool calls.
    tool_calls:
        List of tool-call dicts from the response, or ``None``.
    raw:
        The full OpenAI ``ChatCompletion`` object for advanced use.
    """

    __slots__ = ("content", "tool_calls", "raw")

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        message = raw.choices[0].message
        # OpenAI responses expose content via attribute access.
        self.content: str | None = getattr(message, "content", None) or None
        # Normalise tool_calls — the API may return a list or None.
        tc = getattr(message, "tool_calls", None)
        self.tool_calls: list[Any] | None = list(tc) if tc else None

    def __str__(self) -> str:
        return self.content or ""


# ---------------------------------------------------------------------------
# Platform-mode wrappers (Responses API + moderations)
# ---------------------------------------------------------------------------


# Pattern OGX embeds in refusal text, eg
# "(flagged for: eval-with-expression, insecure-eval-use)".
_FLAGGED_FOR_RE = re.compile(r"\(flagged for:\s*([^)]+)\)", re.IGNORECASE)


def _mcp_servers_to_tools(servers: list[PlatformMcpServer]) -> list[dict[str, Any]]:
    """Translate ``PlatformMcpServer`` entries into the ``tools`` array
    shape expected by ``/v1/responses``.

    Either ``connector_id`` or ``server_url`` is emitted per entry,
    matching the validator on ``PlatformMcpServer``. ``server_label``
    is always set from ``name`` so OGX traces and logs have a stable
    human identifier. ``authorization`` is forwarded when set.
    """
    out: list[dict[str, Any]] = []
    for srv in servers:
        entry: dict[str, Any] = {"type": "mcp", "server_label": srv.name}
        if srv.connector_id:
            entry["connector_id"] = srv.connector_id
        elif srv.url:
            entry["server_url"] = srv.url
        if srv.authorization:
            entry["authorization"] = srv.authorization
        out.append(entry)
    return out


def _extract_refusal(output: list[Any]) -> str | None:
    """Return the first ``refusal`` string found in a Responses ``output``
    array, or ``None``.

    OGX signals a fired guardrail by replacing ``output[*].content[*]``
    with ``{"type":"refusal","refusal":"..."}`` in the terminal payload.
    """
    for item in output or []:
        content = _attr_or_key(item, "content") or []
        for part in content:
            if _attr_or_key(part, "type") == "refusal":
                refusal = _attr_or_key(part, "refusal")
                if isinstance(refusal, str):
                    return refusal
    return None


def _attr_or_key(obj: Any, name: str) -> Any:
    """Look up *name* on a Pydantic-style object or a dict transparently.

    The OpenAI SDK returns typed objects from ``responses.create()``;
    streaming events arrive as dataclass-like objects; we also feed
    fixture dicts in tests. This helper tolerates all three.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _shield_id_from_refusal(
    refusal: str,
    configured: list[str],
) -> str:
    """Best-effort attribution of which shield fired.

    OGX does not return a machine-readable shield ID alongside the
    refusal — it embeds the violation type in the message. We parse
    ``flagged for: ...)`` first; on miss, fall back to the configured
    shield list joined with commas (informative even when ambiguous).
    """
    match = _FLAGGED_FOR_RE.search(refusal or "")
    if match:
        return match.group(1).strip()
    if configured:
        return ",".join(configured)
    return "unknown"


class PlatformResponse:
    """Thin wrapper around an OGX ``/v1/responses`` non-streaming reply.

    Attributes
    ----------
    content:
        Joined ``output_text`` content from all assistant message parts,
        or ``None`` when the response was a refusal or was empty.
    refusal:
        The refusal string when a guardrail fired, otherwise ``None``.
    response_id:
        OGX-assigned response identifier.
    usage:
        Raw usage block (``input_tokens``, ``output_tokens``,
        ``total_tokens``, plus ``input_tokens_details`` /
        ``output_tokens_details`` when present).
    raw:
        The full SDK response object for advanced use.
    """

    __slots__ = ("content", "refusal", "response_id", "usage", "raw")

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        output = _attr_or_key(raw, "output") or []
        self.refusal: str | None = _extract_refusal(output)

        text_parts: list[str] = []
        if self.refusal is None:
            for item in output:
                content = _attr_or_key(item, "content") or []
                for part in content:
                    if _attr_or_key(part, "type") == "output_text":
                        text = _attr_or_key(part, "text")
                        if isinstance(text, str):
                            text_parts.append(text)
        self.content: str | None = "".join(text_parts) if text_parts else None

        self.response_id: str | None = _attr_or_key(raw, "id")
        self.usage: Any = _attr_or_key(raw, "usage")

    def __str__(self) -> str:
        return self.content or self.refusal or ""


@dataclasses.dataclass
class ModerationResult:
    """Result of a single ``/v1/moderations`` call.

    ``flagged`` is the ``OR`` of per-result flags. ``categories`` and
    ``category_scores`` aggregate across all results. ``model`` is the
    moderation model OGX reported.
    """

    flagged: bool
    categories: dict[str, bool]
    category_scores: dict[str, float]
    model: str
    raw: Any


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------


class LLMClient:
    """Async LLM client backed by the OpenAI SDK.

    Parameters
    ----------
    config:
        An ``LLMConfig`` instance (from ``agent.yaml``).  Provides model
        name, endpoint URL, temperature, and max_tokens.
    platform:
        Optional :class:`PlatformConfig`. When set with
        ``platform.enabled=True``, the client lazily creates a second
        ``AsyncOpenAI`` pointed at ``platform.endpoint`` for the
        Responses API and moderations methods. The chat-completions
        client and endpoint are unchanged.
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        platform: PlatformConfig | None = None,
    ) -> None:
        self._config = config
        self._platform = platform
        self._client = AsyncOpenAI(
            base_url=config.endpoint or None,
            api_key=os.environ.get("OPENAI_API_KEY", "not-required"),
        )
        self._platform_client: AsyncOpenAI | None = None

    def _require_platform(self) -> AsyncOpenAI:
        """Return (lazily building) the AsyncOpenAI client for the platform endpoint."""
        if self._platform is None or not self._platform.enabled:
            raise LLMError(
                "Platform-mode method called but platform.enabled is false. "
                "Set platform.enabled=true and platform.endpoint in agent.yaml."
            )
        if self._platform_client is None:
            self._platform_client = AsyncOpenAI(
                base_url=self._platform.endpoint,
                api_key=os.environ.get("OPENAI_API_KEY", "not-required"),
            )
        return self._platform_client

    # -- internal helpers ---------------------------------------------------

    def _base_kwargs(self, **overrides: Any) -> dict[str, Any]:
        """Build the kwargs dict that every completion call starts from."""
        # Strip legacy litellm provider prefixes (e.g. "openai/model" -> "model")
        model_name = self._config.name
        if "/" in model_name:
            prefix = model_name.split("/", 1)[0].lower()
            if prefix in ("openai", "vllm", "llamastack"):
                model_name = model_name.split("/", 1)[1]
        kwargs: dict[str, Any] = {
            "model": model_name,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        kwargs.update(overrides)
        return kwargs

    async def _acompletion(self, **kwargs: Any) -> Any:
        """Call the OpenAI chat completions API and translate exceptions."""
        try:
            return await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMError(
                f"LLM call failed ({type(exc).__name__}): {exc}"
            ) from exc

    # -- public API ---------------------------------------------------------

    async def call_model(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Standard chat completion.

        Parameters
        ----------
        messages:
            OpenAI-format message list.
        tools:
            Optional list of tool schemas for function calling.
        **kwargs:
            Extra keyword arguments forwarded to the chat completions API.

        Returns
        -------
        ModelResponse:
            Wrapper with ``.content``, ``.tool_calls``, and ``.raw``.
        """
        call_kwargs = self._base_kwargs(**kwargs)
        call_kwargs["messages"] = messages
        if tools is not None:
            call_kwargs["tools"] = tools
        raw = await self._acompletion(**call_kwargs)
        return ModelResponse(raw)

    async def call_model_json(
        self,
        messages: list[dict[str, Any]],
        schema: type[BaseModel] | dict[str, Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> BaseModel | dict[str, Any]:
        """Structured-output completion.

        Requests JSON conforming to *schema* and returns a parsed object.
        When *schema* is a Pydantic model class the return value is an
        instance of that class.  When it is a raw JSON-schema dict the
        return value is a plain dict.

        Parameters
        ----------
        messages:
            OpenAI-format message list.
        schema:
            A Pydantic model class **or** a JSON-schema dict.
        tools:
            Optional tool schemas for function calling.
        **kwargs:
            Extra keyword arguments forwarded to the chat completions API.
        """
        response_format = _schema_to_response_format(schema)
        call_kwargs = self._base_kwargs(**kwargs)
        call_kwargs["messages"] = messages
        call_kwargs["response_format"] = response_format
        if tools is not None:
            call_kwargs["tools"] = tools
        raw = await self._acompletion(**call_kwargs)
        content = raw.choices[0].message.content
        if content is None:
            raise LLMError(
                "Model returned no content in structured-output mode"
            )
        return _parse_json_response(content, schema)

    async def call_model_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Streaming chat completion (content-only).

        Yields content-delta strings as they arrive from the provider.
        Discards reasoning, tool calls, and other delta fields. Use
        ``call_model_stream_raw`` if you need the full chunk.

        Parameters
        ----------
        messages:
            OpenAI-format message list.
        tools:
            Optional tool schemas for function calling.
        **kwargs:
            Extra keyword arguments forwarded to the chat completions API.
        """
        async for chunk in self.call_model_stream_raw(
            messages, tools=tools, **kwargs
        ):
            try:
                delta = chunk.choices[0].delta
            except (AttributeError, IndexError):
                continue
            content = getattr(delta, "content", None)
            if content:
                yield content

    async def call_model_stream_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Streaming chat completion (raw chunks).

        Yields the full streaming chunk for each delta. Callers can
        inspect ``chunk.choices[0].delta`` for ``content``, ``role``,
        ``tool_calls``, ``reasoning_content``, and other provider fields.
        Used by ``BaseAgent.astep_stream`` to drive rich streaming with
        thinking, tool execution, and response phases preserved.

        Parameters
        ----------
        messages:
            OpenAI-format message list.
        tools:
            Optional tool schemas for function calling.
        **kwargs:
            Extra keyword arguments forwarded to the chat completions API.
        """
        call_kwargs = self._base_kwargs(**kwargs)
        call_kwargs["messages"] = messages
        call_kwargs["stream"] = True
        # Opt into vLLM/OpenAI usage chunk on the terminal stream event so
        # the server's cost-tracking accumulator sees prompt/completion
        # tokens. setdefault keeps caller-supplied stream_options intact.
        call_kwargs.setdefault("stream_options", {"include_usage": True})
        if tools is not None:
            call_kwargs["tools"] = tools
        try:
            response = await self._client.chat.completions.create(**call_kwargs)
        except Exception as exc:
            raise LLMError(
                f"LLM streaming call failed ({type(exc).__name__}): {exc}"
            ) from exc
        try:
            async for chunk in response:
                yield chunk
        except Exception as exc:
            raise LLMError(
                f"Error during streaming iteration ({type(exc).__name__}): {exc}"
            ) from exc

    async def call_model_validated(
        self,
        messages: list[dict[str, Any]],
        validator_fn: Callable[[ModelResponse], T],
        *,
        max_retries: int = 3,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> T:
        """Call the model, pass the result to a validator, and retry on failure.

        Calls ``call_model`` and feeds the ``ModelResponse`` to
        *validator_fn*.  If the validator raises, the call is retried with
        exponential backoff until *max_retries* attempts are exhausted.

        Parameters
        ----------
        messages:
            OpenAI-format message list.
        validator_fn:
            A callable that receives the ``ModelResponse`` and returns the
            validated result.  Should raise any exception to signal that
            the response is invalid and the call should be retried.
        max_retries:
            Maximum number of retry attempts after the initial call.
            Defaults to 3 (so up to 4 total attempts).
        tools:
            Optional tool schemas for function calling.
        **kwargs:
            Extra keyword arguments forwarded to ``call_model``.

        Returns
        -------
        T:
            Whatever ``validator_fn`` returns on success.

        Raises
        ------
        LLMError:
            If all attempts are exhausted without a valid response.
        """
        last_error: Exception | None = None
        total_attempts = 1 + max_retries

        for attempt in range(total_attempts):
            response = await self.call_model(
                messages, tools=tools, **kwargs
            )
            try:
                return validator_fn(response)
            except Exception as exc:
                last_error = exc
                if attempt < total_attempts - 1:
                    delay = (2 ** attempt) * 1.0  # 1s, 2s, 4s, ...
                    logger.warning(
                        "Validation failed (attempt %d/%d): %s — "
                        "retrying in %.1fs",
                        attempt + 1,
                        total_attempts,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise LLMError(
            f"Validation failed after {total_attempts} attempts. "
            f"Last error: {last_error}"
        ) from last_error

    # -- platform mode (Responses API + moderations) -----------------------

    def _platform_base_kwargs(self, **overrides: Any) -> dict[str, Any]:
        """Build the kwargs dict shared by all ``/v1/responses`` calls."""
        model_name = self._config.name
        if "/" in model_name:
            prefix = model_name.split("/", 1)[0].lower()
            # Note: in OGX, the "vllm/RedHatAI/gpt-oss-20b" form is the
            # registered model id and must be passed verbatim. Only strip
            # the legacy "openai/" prefix.
            if prefix == "openai":
                model_name = model_name.split("/", 1)[1]
        kwargs: dict[str, Any] = {
            "model": model_name,
            "temperature": self._config.temperature,
        }
        kwargs.update(overrides)
        return kwargs

    async def call_model_responses(
        self,
        input: str | list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        guardrails: list[str] | None = None,
        **kwargs: Any,
    ) -> PlatformResponse:
        """Non-streaming ``/v1/responses`` call.

        Parameters
        ----------
        input:
            Either a plain user-message string or an OpenAI-format
            message list. Forwarded directly to OGX.
        tools:
            Pre-built ``tools`` array (eg from
            :func:`_mcp_servers_to_tools`). When ``None``, defaults to
            the configured ``platform.mcp`` translated to wire format.
        guardrails:
            Shield IDs to enforce on this call. When ``None``, defaults
            to the configured ``platform.guardrails``. Pass an empty
            list to disable for one call.
        **kwargs:
            Extra fields forwarded to the Responses API.

        Returns
        -------
        PlatformResponse:
            Wrapper exposing ``content``, ``refusal``, ``usage``, and
            ``raw``.
        """
        client = self._require_platform()
        platform = self._platform  # type: ignore[union-attr]
        if tools is None:
            tools = _mcp_servers_to_tools(platform.mcp)  # type: ignore[union-attr]
        if guardrails is None:
            guardrails = list(platform.guardrails)  # type: ignore[union-attr]

        call_kwargs = self._platform_base_kwargs(**kwargs)
        call_kwargs["input"] = input
        if tools:
            call_kwargs["tools"] = tools
        if guardrails:
            # `guardrails` is an OGX extension to the Responses API and is
            # not recognised by the OpenAI SDK's typed kwargs. Route it
            # through extra_body so the SDK forwards it verbatim while
            # preserving any caller-supplied extra_body keys.
            extra_body = dict(call_kwargs.pop("extra_body", None) or {})
            extra_body["guardrails"] = guardrails
            call_kwargs["extra_body"] = extra_body

        try:
            raw = await client.responses.create(**call_kwargs)
        except Exception as exc:
            raise LLMError(
                f"Responses API call failed ({type(exc).__name__}): {exc}"
            ) from exc
        return PlatformResponse(raw)

    async def call_model_responses_stream(
        self,
        input: str | list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        guardrails: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Streaming ``/v1/responses`` call mapped onto :class:`StreamEvent`.

        Emits a stream of typed events:

        - ``ContentDelta`` for every ``response.output_text.delta``
        - ``GuardrailFiredEvent`` when the terminal payload contains a
          refusal (``content[].type == "refusal"``); ``shield_id`` is
          parsed from ``flagged for: ...`` when present, else falls back
          to the configured shield list joined by commas.
        - Terminal ``StreamComplete`` with ``finish_reason="guardrail"``
          when a refusal was emitted, otherwise ``"stop"``.

        Tool-call events (``response.tool_call.*``) are not yet mapped —
        a follow-up commit will wire them once OGX-orchestrated MCP tool
        calls can be exercised end-to-end.

        Note on UX: when a guardrail fires *after* generation has begun,
        OGX still streams the unsafe ``output_text.delta`` events before
        replacing the terminal payload with the refusal. We pass those
        deltas through; consumers that need post-shield content only
        should buffer until ``StreamComplete``.
        """
        client = self._require_platform()
        platform = self._platform  # type: ignore[union-attr]
        if tools is None:
            tools = _mcp_servers_to_tools(platform.mcp)  # type: ignore[union-attr]
        if guardrails is None:
            guardrails = list(platform.guardrails)  # type: ignore[union-attr]

        call_kwargs = self._platform_base_kwargs(**kwargs)
        call_kwargs["input"] = input
        call_kwargs["stream"] = True
        if tools:
            call_kwargs["tools"] = tools
        if guardrails:
            # See the note in call_model_responses — guardrails is an OGX
            # extension and must travel through extra_body.
            extra_body = dict(call_kwargs.pop("extra_body", None) or {})
            extra_body["guardrails"] = guardrails
            call_kwargs["extra_body"] = extra_body

        configured_guardrails = list(guardrails)

        try:
            response = await client.responses.create(**call_kwargs)
        except Exception as exc:
            raise LLMError(
                f"Responses streaming call failed ({type(exc).__name__}): {exc}"
            ) from exc

        guardrail_emitted = False
        usage: Any = None
        try:
            async for event in response:
                event_type = _attr_or_key(event, "type")

                if event_type == "response.output_text.delta":
                    delta = _attr_or_key(event, "delta")
                    if isinstance(delta, str) and delta:
                        yield ContentDelta(content=delta)

                elif event_type == "response.completed":
                    final = _attr_or_key(event, "response")
                    output = _attr_or_key(final, "output") or []
                    refusal = _extract_refusal(output)
                    usage = _attr_or_key(final, "usage")
                    if refusal is not None:
                        guardrail_emitted = True
                        yield GuardrailFiredEvent(
                            shield_id=_shield_id_from_refusal(
                                refusal, configured_guardrails
                            ),
                            action="blocked",
                            message=refusal,
                        )
                    break

                # Other event types (response.created, in_progress,
                # output_item.added, content_part.added/done,
                # output_item.done, plus future tool-call events) are
                # state transitions we don't yet surface as StreamEvents.
        except Exception as exc:
            raise LLMError(
                f"Error during Responses streaming iteration "
                f"({type(exc).__name__}): {exc}"
            ) from exc

        metrics = StreamMetrics()
        if usage is not None:
            metrics.prompt_tokens = _attr_or_key(usage, "input_tokens")
            metrics.completion_tokens = _attr_or_key(usage, "output_tokens")
            metrics.total_tokens = _attr_or_key(usage, "total_tokens")
        metrics.model_calls = 1
        finish_reason = "guardrail" if guardrail_emitted else "stop"
        yield StreamComplete(finish_reason=finish_reason, metrics=metrics)

    async def moderate(
        self,
        content: str,
        *,
        model: str,
    ) -> ModerationResult:
        """Classify *content* via ``/v1/moderations``.

        Parameters
        ----------
        content:
            Text to classify.
        model:
            Moderation model identifier (in OGX, this is typically a
            registered shield id, eg ``"code-scanner"``).

        Returns
        -------
        ModerationResult:
            Aggregated ``flagged`` / ``categories`` / ``category_scores``
            across results, plus the raw SDK object.
        """
        client = self._require_platform()
        try:
            raw = await client.moderations.create(input=content, model=model)
        except Exception as exc:
            raise LLMError(
                f"Moderations API call failed ({type(exc).__name__}): {exc}"
            ) from exc

        results = _attr_or_key(raw, "results") or []
        flagged = False
        categories: dict[str, bool] = {}
        category_scores: dict[str, float] = {}
        for result in results:
            if _attr_or_key(result, "flagged"):
                flagged = True
            r_categories = _attr_or_key(result, "categories") or {}
            r_scores = _attr_or_key(result, "category_scores") or {}
            if isinstance(r_categories, dict):
                for k, v in r_categories.items():
                    categories[k] = bool(v) or categories.get(k, False)
            if isinstance(r_scores, dict):
                for k, v in r_scores.items():
                    if isinstance(v, (int, float)):
                        category_scores[k] = max(category_scores.get(k, 0.0), float(v))

        return ModerationResult(
            flagged=flagged,
            categories=categories,
            category_scores=category_scores,
            model=str(_attr_or_key(raw, "model") or model),
            raw=raw,
        )
