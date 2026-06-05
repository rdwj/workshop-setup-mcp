"""Cost computation from token counts and per-model pricing rates.

Pricing is configured in ``agent.yaml`` under the top-level ``pricing`` key.
Self-hosted vLLM deployments rarely match OpenAI's published per-token rates,
so the schema supports a per-model lookup table with a ``default`` fallback.

The single public entry point :func:`compute_cost` is pure -- given a model
name, token counts, and a :class:`PricingConfig`, it returns USD cost. It
makes no I/O and does not consult any global state, so it is trivially safe
to call from observers, REST handlers, or budget enforcement code paths.

.. deprecated:: 0.27.0
   Import from :mod:`fipsagents.baseagent.pricing` instead. This module
   re-exports for backward compatibility but will be removed in 1.0.
"""

from __future__ import annotations

from fipsagents.baseagent.config import PricingConfig, PricingRate
from fipsagents.baseagent.pricing import compute_cost, rate_for_model


__all__ = [
    "PricingConfig",
    "PricingRate",
    "compute_cost",
    "rate_for_model",
]
