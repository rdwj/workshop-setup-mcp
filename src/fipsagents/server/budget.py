"""Cost budget enforcement for chat completion requests.

:class:`BudgetEnforcer` follows the same observer pattern as
:class:`~fipsagents.server.metrics.MetricsCollector` and
:class:`~fipsagents.server.collector.TraceCollector` — it sits beside
the chat-completion code path and reads/records cost without modifying
the agent's behaviour.

Two budget scopes:

- **Per-session**: cumulative session cost lives in ``cost_data`` on
  the session store. The enforcer reads it before each request, converts
  to USD via :class:`~fipsagents.baseagent.config.PricingConfig`, and
  compares to ``budget.per_session.limit_usd``. Works across processes
  (any agent replica that loads the same session sees the same total).
- **Per-tenant**: aggregated *in-process* by accumulating per-turn
  session-cost deltas keyed by ``tenant_id``.  Accurate for
  single-replica deployments and represents "this agent process's view"
  of cross-session tenant cost.  Multi-replica tenant aggregation
  requires a separate cross-agent service and is out of scope.

Hard limits raise :class:`BudgetExceededError`; the server maps that to
HTTP 402 Payment Required so callers can distinguish budget rejection
from rate-limit (429) and auth (401/403) failures. Soft warnings emit
a single ``WARNING``-level log line per crossing per scope, then go
quiet so a long-lived warned session doesn't spam logs.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from fipsagents.baseagent.config import BudgetConfig, PricingConfig

from .pricing import compute_cost

if TYPE_CHECKING:
    from .sessions import SessionStore

logger = logging.getLogger(__name__)


__all__ = ["BudgetEnforcer", "BudgetExceededError", "NullBudgetEnforcer"]


class BudgetExceededError(Exception):
    """Raised when a hard budget limit would be exceeded.

    Attributes:
        scope: ``"session"`` or ``"tenant"``.
        identifier: The session_id or tenant_id whose budget tripped.
        current_usd: Running cost at the moment the limit was checked.
        limit_usd: Configured hard limit.
    """

    def __init__(
        self,
        scope: str,
        identifier: str,
        current_usd: float,
        limit_usd: float,
    ) -> None:
        self.scope = scope
        self.identifier = identifier
        self.current_usd = current_usd
        self.limit_usd = limit_usd
        super().__init__(
            f"{scope} budget exceeded for {identifier!r}: "
            f"${current_usd:.4f} >= ${limit_usd:.4f}"
        )


class BudgetEnforcer:
    """Reads cumulative cost; raises or warns based on configured limits."""

    def __init__(
        self,
        *,
        config: BudgetConfig,
        pricing: PricingConfig,
        session_store: "SessionStore",
    ) -> None:
        self._config = config
        self._pricing = pricing
        self._session_store = session_store
        # tenant_id -> running USD total observed by this agent process.
        self._tenant_costs: dict[str, float] = defaultdict(float)
        # session_id -> last seen cumulative USD (for delta computation).
        self._session_costs: dict[str, float] = {}
        # Sets of (scope, identifier) we've already warned about, to keep
        # logs from spamming when a long session sits over the warn threshold.
        self._session_warned: set[str] = set()
        self._tenant_warned: set[str] = set()

    @property
    def config(self) -> BudgetConfig:
        return self._config

    async def _session_cost_usd(self, session_id: str) -> float:
        """Read the session's cumulative cost_data and convert to USD."""
        try:
            cost_data = await self._session_store.get_cost_data(session_id)
        except NotImplementedError:
            # HTTP store on a platform that doesn't expose the read.
            return 0.0
        if not cost_data:
            return 0.0
        model = cost_data.get("model")
        return compute_cost(
            model,
            input_tokens=int(cost_data.get("input_tokens", 0) or 0),
            output_tokens=int(cost_data.get("output_tokens", 0) or 0),
            cached_tokens=int(cost_data.get("cached_tokens", 0) or 0),
            pricing=self._pricing,
        )

    def _tenant_cost_usd(self, tenant_id: str) -> float:
        return self._tenant_costs.get(tenant_id, 0.0)

    async def check_before_request(
        self,
        *,
        session_id: str | None,
        tenant_id: str | None,
    ) -> None:
        """Raise :class:`BudgetExceededError` if either hard limit is met.

        Called before the chat completion runs. Reads the current
        cumulative session cost from the store; per-tenant uses the
        in-process accumulator. ``observe`` mode degrades both raising
        paths to a log line.
        """
        if not self._config.is_active():
            return

        ses_cfg = self._config.per_session
        if session_id and ses_cfg.limit_usd > 0:
            cost = await self._session_cost_usd(session_id)
            if cost >= ses_cfg.limit_usd:
                self._handle_limit("session", session_id, cost, ses_cfg.limit_usd)

        ten_cfg = self._config.per_tenant
        if tenant_id and ten_cfg.limit_usd > 0:
            cost = self._tenant_cost_usd(tenant_id)
            if cost >= ten_cfg.limit_usd:
                self._handle_limit("tenant", tenant_id, cost, ten_cfg.limit_usd)

    async def record_after_request(
        self,
        *,
        session_id: str | None,
        tenant_id: str | None,
    ) -> None:
        """Refresh in-process tenant total from the new session cost.

        Called after :meth:`OpenAIChatServer._persist_cost_data` writes
        the turn's deltas. The session-cost delta from the store is added
        to the tenant counter so the next pre-check sees the latest
        running tenant total. Also fires soft warnings if either scope
        crossed its ``warn_usd`` threshold during this turn.
        """
        if not self._config.is_active():
            return

        if not session_id:
            return

        new_session_cost = await self._session_cost_usd(session_id)
        old_session_cost = self._session_costs.get(session_id, 0.0)
        delta = max(new_session_cost - old_session_cost, 0.0)
        self._session_costs[session_id] = new_session_cost

        if tenant_id and delta > 0.0:
            self._tenant_costs[tenant_id] = (
                self._tenant_costs.get(tenant_id, 0.0) + delta
            )

        # Soft warnings (one per scope per identifier).
        ses_warn = self._config.per_session.warn_usd
        if ses_warn > 0 and new_session_cost >= ses_warn:
            if session_id not in self._session_warned:
                self._session_warned.add(session_id)
                logger.warning(
                    "Session %s crossed soft budget warning at $%.4f "
                    "(threshold $%.4f)",
                    session_id, new_session_cost, ses_warn,
                )

        if tenant_id:
            tenant_total = self._tenant_costs.get(tenant_id, 0.0)
            ten_warn = self._config.per_tenant.warn_usd
            if ten_warn > 0 and tenant_total >= ten_warn:
                if tenant_id not in self._tenant_warned:
                    self._tenant_warned.add(tenant_id)
                    logger.warning(
                        "Tenant %s crossed soft budget warning at $%.4f "
                        "(threshold $%.4f)",
                        tenant_id, tenant_total, ten_warn,
                    )

    def _handle_limit(
        self,
        scope: str,
        identifier: str,
        current_usd: float,
        limit_usd: float,
    ) -> None:
        """Either raise (enforce) or log (observe) a hard-limit crossing."""
        if self._config.mode == "enforce":
            raise BudgetExceededError(scope, identifier, current_usd, limit_usd)
        logger.warning(
            "Budget %s limit hit for %s: $%.4f >= $%.4f (mode=observe, "
            "request allowed through)",
            scope, identifier, current_usd, limit_usd,
        )


class NullBudgetEnforcer:
    """No-op enforcer used when :class:`BudgetConfig` has no active limits.

    Lets the chat completion code call ``check_before_request`` /
    ``record_after_request`` unconditionally without paying for the store
    read on every turn.
    """

    async def check_before_request(
        self,
        *,
        session_id: str | None,  # noqa: ARG002
        tenant_id: str | None,  # noqa: ARG002
    ) -> None:
        return None

    async def record_after_request(
        self,
        *,
        session_id: str | None,  # noqa: ARG002
        tenant_id: str | None,  # noqa: ARG002
    ) -> None:
        return None


def create_budget_enforcer(
    config: BudgetConfig,
    *,
    pricing: PricingConfig,
    session_store: "SessionStore",
) -> BudgetEnforcer | NullBudgetEnforcer:
    """Return a real enforcer when limits are configured, no-op otherwise."""
    if not config.is_active():
        return NullBudgetEnforcer()
    return BudgetEnforcer(
        config=config, pricing=pricing, session_store=session_store,
    )
