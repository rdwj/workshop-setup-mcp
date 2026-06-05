"""Cost computation from token counts and per-model pricing rates.

Pricing is configured in ``agent.yaml`` under the top-level ``pricing`` key.
Self-hosted vLLM deployments rarely match OpenAI's published per-token rates,
so the schema supports a per-model lookup table with a ``default`` fallback.

The public entry points :func:`compute_cost` and :func:`rate_for_model` are
pure functions -- given a model name, token counts, and a :class:`PricingConfig`,
they return USD cost or the applicable rate. They make no I/O and do not consult
any global state, so they are trivially safe to call from observers, REST handlers,
or budget enforcement code paths.
"""

from __future__ import annotations

from fipsagents.baseagent.config import PricingConfig, PricingRate


__all__ = [
    "compute_cost",
    "rate_for_model",
]


def rate_for_model(model_name: str | None, pricing: PricingConfig) -> PricingRate:
    """Return the :class:`PricingRate` that applies to *model_name*.

    Lookup is exact match against ``pricing.models`` keys; falls back to
    ``pricing.default`` when no match is found or the model name is empty.
    """
    if model_name and model_name in pricing.models:
        return pricing.models[model_name]
    return pricing.default


def compute_cost(
    model_name: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    pricing: PricingConfig,
) -> float:
    """Compute USD cost for a turn (or cumulative session) of token usage.

    Cached tokens follow OpenAI semantics: they are counted as a *subset*
    of ``input_tokens`` and billed at the discounted ``cached_input_per_1k``
    rate when one is configured. When ``cached_input_per_1k`` is ``None``
    the ``cached_tokens`` value is ignored and full input rate applies.

    Returns 0.0 when the resolved rate has no non-zero pricing fields,
    making this safe to call unconditionally for self-hosted models that
    have no real dollar cost.
    """
    rate = rate_for_model(model_name, pricing)

    cost = 0.0
    cost += (max(input_tokens, 0) / 1000.0) * rate.input_per_1k
    cost += (max(output_tokens, 0) / 1000.0) * rate.output_per_1k

    if cached_tokens and rate.cached_input_per_1k is not None:
        # Cached tokens are a subset of input_tokens; refund the input
        # rate and re-bill at the cached rate so we don't double-charge.
        clamped_cached = min(max(cached_tokens, 0), max(input_tokens, 0))
        cost -= (clamped_cached / 1000.0) * rate.input_per_1k
        cost += (clamped_cached / 1000.0) * rate.cached_input_per_1k

    cost += rate.per_request
    return round(cost, 6)
