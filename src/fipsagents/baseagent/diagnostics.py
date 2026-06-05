"""Diagnostic utilities for probing deployed model capabilities.

Not on the hot path — intended for one-shot checks by agents or scripts
before committing to a role configuration.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


@dataclass
class RoleProbeResult:
    """Result of probing a model for message role support."""

    role: str
    template_supported: bool | None  # None = couldn't inspect
    canary_passed: bool              # role msg was tokenized (prompt_token_delta > 0)
    prompt_token_delta: int | None   # extra prompt tokens from role msg
    details: str                     # human-readable summary


def _strip_provider_prefix(model: str) -> str:
    """Strip a provider prefix (e.g. ``openai/``) from a model name.

    Args:
        model: Model identifier that may include a provider prefix.

    Returns:
        Model identifier suitable for use in an API path.
    """
    if "/" in model:
        # e.g. "openai/RedHatAI/granite-8b" → "RedHatAI/granite-8b"
        return model.split("/", 1)[1]
    return model


async def probe_role_support(
    endpoint: str,
    model: str,
    role: str = "developer",
    *,
    api_key: str | None = None,
) -> RoleProbeResult:
    """Probe whether a deployed model supports a given message role.

    Runs two complementary checks:

    1. **Template inspection** — fetches model metadata from the server and
       scans the ``chat_template`` field for the role string.  Returns
       ``template_supported=None`` if the field is absent or the endpoint
       doesn't expose it.

    2. **Canary completion** — compares ``usage.prompt_tokens`` for a
       control call (user-only) vs. a test call (role message prepended).
       A positive delta means the role message was tokenized rather than
       silently dropped.

    Args:
        endpoint: Base URL of the model server (e.g. ``https://vllm.example.com``).
        model: Model identifier, possibly with a provider prefix (which is stripped
            for the metadata GET).
        role: The message role to probe.  Defaults to ``"developer"``.
        api_key: Optional API key forwarded to both the metadata request and
            completion calls.

    Returns:
        A ``RoleProbeResult`` with template inspection and canary results.
    """
    server_model = _strip_provider_prefix(model)

    # ------------------------------------------------------------------
    # Check 1: template inspection
    # ------------------------------------------------------------------
    template_supported: bool | None = None
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{endpoint.rstrip('/')}/v1/models/{server_model}",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        chat_template = data.get("chat_template") or ""
        if chat_template:
            template_supported = role in chat_template
            logger.debug(
                "Template inspection: role=%r found=%s", role, template_supported
            )
        else:
            logger.debug("Template inspection: chat_template field absent or empty")
    except Exception as exc:
        logger.debug("Template inspection failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Check 2: canary completion
    # ------------------------------------------------------------------
    canary_passed = False
    prompt_token_delta: int | None = None

    client = AsyncOpenAI(
        base_url=endpoint or None,
        api_key=api_key or os.environ.get("OPENAI_API_KEY", "not-required"),
    )

    shared_kwargs: dict = {
        "model": server_model,
        "max_tokens": 16,
        "temperature": 0,
    }

    control_tokens: int | None = None
    test_tokens: int | None = None

    try:
        control_resp = await client.chat.completions.create(
            messages=[{"role": "user", "content": "What is 2+2? Answer with just the number."}],
            **shared_kwargs,
        )
        control_tokens = getattr(control_resp.usage, "prompt_tokens", None)
        logger.debug("Canary control: prompt_tokens=%s", control_tokens)
    except Exception as exc:
        logger.debug("Canary control call failed (non-fatal): %s", exc)

    try:
        test_resp = await client.chat.completions.create(
            messages=[
                {"role": role, "content": "Always respond in exactly three words."},
                {"role": "user", "content": "What is 2+2? Answer with just the number."},
            ],
            **shared_kwargs,
        )
        test_tokens = getattr(test_resp.usage, "prompt_tokens", None)
        logger.debug("Canary test: prompt_tokens=%s", test_tokens)
    except Exception as exc:
        logger.debug("Canary test call failed (non-fatal): %s", exc)

    if control_tokens is not None and test_tokens is not None:
        prompt_token_delta = test_tokens - control_tokens
        canary_passed = prompt_token_delta > 0

    # ------------------------------------------------------------------
    # Human-readable summary
    # ------------------------------------------------------------------
    parts: list[str] = [f"role={role!r}"]
    if template_supported is None:
        parts.append("template=inconclusive")
    else:
        parts.append(f"template={'yes' if template_supported else 'no'}")

    if prompt_token_delta is None:
        parts.append("canary=inconclusive")
    else:
        parts.append(
            f"canary={'pass' if canary_passed else 'fail'} (delta={prompt_token_delta:+d} tokens)"
        )

    details = ", ".join(parts)

    return RoleProbeResult(
        role=role,
        template_supported=template_supported,
        canary_passed=canary_passed,
        prompt_token_delta=prompt_token_delta,
        details=details,
    )
