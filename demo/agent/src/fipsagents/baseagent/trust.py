"""Trust accumulation, decay, and level transitions.

Tracks agent trust score and level based on work-item outcomes.
Completions increase trust; failures and violations decrease it.
Level transitions happen at configurable thresholds, with demotion
at 50% of the threshold that granted the current level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fipsagents.baseagent.events import TrustLevelChanged

logger = logging.getLogger(__name__)


@dataclass
class TrustEvent:
    """A single trust-affecting event in the history log."""

    timestamp: str
    event_type: str  # "completion", "failure", "violation", "promotion", "demotion"
    delta: float
    reason: str
    resulting_level: int
    resulting_score: float


@dataclass
class TrustState:
    """Snapshot of an agent's trust standing."""

    level: int = 0
    score: float = 0.0
    completions: int = 0
    failures: int = 0
    violations: int = 0
    last_promotion: str | None = None
    last_decay: str | None = None
    history: list[TrustEvent] = field(default_factory=list)


class TrustManager:
    """Manages trust score accumulation, decay, and level transitions.

    Trust levels range from 0 (untrusted) to 4 (fully trusted).
    Each level has a score threshold configured via ``thresholds``.
    Promotion happens when the score crosses the next level's threshold.
    Demotion happens when the score drops below 50% of the current
    level's threshold.

    Parameters
    ----------
    thresholds:
        Score needed to reach levels 1, 2, 3, and 4 respectively.
    state:
        Optional pre-existing trust state (e.g. loaded from checkpoint).
    """

    COMPLETION_BASE = 1.0
    FAILURE_BASE = -5.0
    VIOLATION_BASE = -50.0
    MAX_HISTORY = 100

    def __init__(
        self,
        *,
        thresholds: tuple[float, float, float, float] = (10.0, 50.0, 200.0, 500.0),
        state: TrustState | None = None,
    ) -> None:
        self._thresholds = thresholds
        self._state = state or TrustState()
        self._pending_events: list[TrustLevelChanged] = []

    def record_completion(
        self, *, quality_score: float = 1.0, reason: str = ""
    ) -> TrustState:
        """Record a successful work-item completion.

        ``quality_score`` scales the base completion delta (1.0 = full
        credit, 0.5 = half credit, etc.). Clamped to >= 0.
        """
        delta = self.COMPLETION_BASE * max(0.0, quality_score)
        self._state.score = max(0.0, self._state.score + delta)
        self._state.completions += 1
        self._add_event("completion", delta, reason or "work item completed")
        self._check_promotion()
        return self._state

    def record_failure(
        self, *, severity: float = 1.0, reason: str = ""
    ) -> TrustState:
        """Record a work-item failure.

        ``severity`` scales the penalty (1.0 = full, higher = worse).
        Score is clamped to >= 0.
        """
        delta = self.FAILURE_BASE * max(0.0, severity)
        self._state.score = max(0.0, self._state.score + delta)
        self._state.failures += 1
        self._state.last_decay = datetime.now(timezone.utc).isoformat()
        self._add_event("failure", delta, reason or "work item failed")
        self._check_demotion()
        return self._state

    def record_violation(
        self, *, severity: float = 1.0, reason: str = ""
    ) -> TrustState:
        """Record a trust violation (e.g. security breach, policy violation).

        Violations carry a much steeper penalty than failures. Score is
        clamped to >= 0.
        """
        delta = self.VIOLATION_BASE * max(0.0, severity)
        self._state.score = max(0.0, self._state.score + delta)
        self._state.violations += 1
        self._state.last_decay = datetime.now(timezone.utc).isoformat()
        self._add_event("violation", delta, reason or "trust violation")
        self._check_demotion()
        return self._state

    def get_state(self) -> TrustState:
        """Return the current trust state."""
        return self._state

    @property
    def level(self) -> int:
        """Current trust level (0-4)."""
        return self._state.level

    @property
    def score(self) -> float:
        """Current trust score."""
        return self._state.score

    def drain_events(self) -> list[TrustLevelChanged]:
        """Return and clear any pending trust-level-change events."""
        events = self._pending_events
        self._pending_events = []
        return events

    def seed_from_parent(
        self,
        *,
        parent_trust_level: int,
        capability_overlap: list[str],
        seed_level: int | None = None,
    ) -> TrustState:
        """Seed initial trust from a parent agent's lineage.

        If the parent has trust level >= 3 and there is capability overlap,
        the child starts at trust level 1.  An explicit ``seed_level``
        overrides the automatic logic.
        """
        if seed_level is not None:
            target = min(seed_level, 4)
        elif parent_trust_level >= 3 and capability_overlap:
            target = 1
        else:
            return self._state

        if target > 0 and target <= 4:
            self._state.level = target
            self._state.score = self._thresholds[target - 1]
            self._add_event(
                "seeded",
                0,
                f"trust seeded to level {target} from parent "
                f"(trust={parent_trust_level}, "
                f"overlap={len(capability_overlap)} capabilities)",
            )
            self._pending_events.append(TrustLevelChanged(
                from_level=0,
                to_level=target,
                score=self._state.score,
                reason=f"seeded from parent agent (trust level {parent_trust_level})",
            ))
        return self._state

    # -- Internal helpers ----------------------------------------------------

    def _add_event(self, event_type: str, delta: float, reason: str) -> None:
        """Append a trust event to the history, trimming if over capacity."""
        event = TrustEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            delta=delta,
            reason=reason,
            resulting_level=self._state.level,
            resulting_score=self._state.score,
        )
        self._state.history.append(event)
        if len(self._state.history) > self.MAX_HISTORY:
            self._state.history = self._state.history[-self.MAX_HISTORY :]

    def _check_promotion(self) -> None:
        """Promote if score crosses the next level's threshold."""
        if self._state.level >= 4:
            return
        threshold = self._thresholds[self._state.level]
        if self._state.score >= threshold:
            old_level = self._state.level
            self._state.level += 1
            self._state.last_promotion = datetime.now(timezone.utc).isoformat()
            self._pending_events.append(TrustLevelChanged(
                from_level=old_level,
                to_level=self._state.level,
                score=self._state.score,
                reason=f"promoted from level {old_level} to {self._state.level}",
            ))
            self._add_event(
                "promotion",
                0,
                f"promoted from level {old_level} to {self._state.level}",
            )
            logger.info(
                "Trust level promoted: %d -> %d (score=%.1f)",
                old_level,
                self._state.level,
                self._state.score,
            )

    def _check_demotion(self) -> None:
        """Demote if score drops below 50% of the current level's threshold."""
        if self._state.level <= 0:
            return
        current_threshold = self._thresholds[self._state.level - 1]
        if self._state.score < current_threshold * 0.5:
            old_level = self._state.level
            self._state.level -= 1
            self._pending_events.append(TrustLevelChanged(
                from_level=old_level,
                to_level=self._state.level,
                score=self._state.score,
                reason=f"demoted from level {old_level} to {self._state.level}",
            ))
            self._add_event(
                "demotion",
                0,
                f"demoted from level {old_level} to {self._state.level}",
            )
            logger.warning(
                "Trust level demoted: %d -> %d (score=%.1f)",
                old_level,
                self._state.level,
                self._state.score,
            )
