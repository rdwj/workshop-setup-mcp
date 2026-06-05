"""Agent maturation: stage derivation from trust levels and graduated autonomy.

Maturation stages are derived from trust levels -- there is no independent
maturation state. The TrustManager is the single source of truth.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

from fipsagents.baseagent.events import SkillQuarantined

if TYPE_CHECKING:
    from fipsagents.baseagent.trust import TrustManager

logger = logging.getLogger(__name__)


class MaturationStage(str, enum.Enum):
    """Agent lifecycle stage, derived from trust level."""

    PROTO_AGENT = "proto_agent"
    APPRENTICE = "apprentice"
    JOURNEYMAN = "journeyman"
    SPECIALIST = "specialist"


@dataclass(frozen=True)
class StagePermissions:
    """What an agent at a given stage is allowed to do."""

    can_create_skills: bool
    review_gate: str  # "human_review", "peer_review", "audit_only", "none"
    can_edit_own: bool
    can_delete_own: bool


# Default graduated autonomy table matching the design doc.
_DEFAULT_PERMISSIONS: dict[MaturationStage, StagePermissions] = {
    MaturationStage.PROTO_AGENT: StagePermissions(
        can_create_skills=False,
        review_gate="none",
        can_edit_own=False,
        can_delete_own=False,
    ),
    MaturationStage.APPRENTICE: StagePermissions(
        can_create_skills=True,
        review_gate="human_review",
        can_edit_own=False,
        can_delete_own=False,
    ),
    MaturationStage.JOURNEYMAN: StagePermissions(
        can_create_skills=True,
        review_gate="peer_review",
        can_edit_own=True,
        can_delete_own=False,
    ),
    MaturationStage.SPECIALIST: StagePermissions(
        can_create_skills=True,
        review_gate="audit_only",
        can_edit_own=True,
        can_delete_own=True,
    ),
}


class MaturationManager:
    """Derives maturation stage from trust level and exposes stage-aware queries.

    Maturation stages map to trust level ranges:

    - PROTO_AGENT: trust level 0
    - APPRENTICE: trust levels 1 through ``apprentice_max_trust``
    - JOURNEYMAN: trust levels ``apprentice_max_trust + 1`` through
      ``journeyman_max_trust``
    - SPECIALIST: trust levels >= ``specialist_min_trust``
    """

    def __init__(
        self,
        trust_manager: TrustManager,
        *,
        apprentice_max_trust: int = 1,
        journeyman_max_trust: int = 3,
        specialist_min_trust: int = 4,
    ) -> None:
        self._trust = trust_manager
        self._apprentice_max = apprentice_max_trust
        self._journeyman_max = journeyman_max_trust
        self._specialist_min = specialist_min_trust

    def current_stage(self) -> MaturationStage:
        """Derive the current maturation stage from trust level."""
        level = self._trust.level
        if level >= self._specialist_min:
            return MaturationStage.SPECIALIST
        if level > self._apprentice_max:
            return MaturationStage.JOURNEYMAN
        if level >= 1:
            return MaturationStage.APPRENTICE
        return MaturationStage.PROTO_AGENT

    def get_permissions(
        self, stage: MaturationStage | None = None
    ) -> StagePermissions:
        """Get the permissions for a stage (defaults to current stage)."""
        if stage is None:
            stage = self.current_stage()
        return _DEFAULT_PERMISSIONS[stage]

    def promotion_progress(self) -> dict:
        """Return progress toward the next maturation stage."""
        stage = self.current_stage()
        level = self._trust.level
        score = self._trust.score

        if stage == MaturationStage.SPECIALIST:
            return {
                "current_stage": stage.value,
                "next_stage": None,
                "trust_level": level,
                "trust_score": score,
                "threshold_for_next": None,
                "pct_complete": 100.0,
            }

        # Determine the trust-score threshold for the next stage.
        thresholds = self._trust._thresholds
        if stage == MaturationStage.PROTO_AGENT:
            next_stage = MaturationStage.APPRENTICE
            threshold = thresholds[0]  # level-1 threshold
        elif stage == MaturationStage.APPRENTICE:
            next_stage = MaturationStage.JOURNEYMAN
            threshold = thresholds[self._apprentice_max]
        else:  # JOURNEYMAN
            next_stage = MaturationStage.SPECIALIST
            threshold = thresholds[self._specialist_min - 1]

        pct = (
            min(100.0, (score / threshold * 100.0)) if threshold > 0 else 100.0
        )

        return {
            "current_stage": stage.value,
            "next_stage": next_stage.value,
            "trust_level": level,
            "trust_score": score,
            "threshold_for_next": threshold,
            "pct_complete": round(pct, 1),
        }

    def get_summary(self) -> dict:
        """Full maturation summary for the REST API."""
        stage = self.current_stage()
        perms = self.get_permissions(stage)
        progress = self.promotion_progress()
        state = self._trust.get_state()

        return {
            "stage": stage.value,
            "permissions": {
                "can_create_skills": perms.can_create_skills,
                "review_gate": perms.review_gate,
                "can_edit_own": perms.can_edit_own,
                "can_delete_own": perms.can_delete_own,
            },
            "trust": {
                "level": state.level,
                "score": state.score,
                "completions": state.completions,
                "failures": state.failures,
                "violations": state.violations,
            },
            "progress": progress,
        }


def quarantine_out_of_scope_skills(
    learned_dir: Path,
    trust_level: int,
    trust_domains: list[str],
) -> list[SkillQuarantined]:
    """Mark learned skills outside the agent's current trust scope as quarantined.

    Scans every skill in *learned_dir*, compares its ``domain`` against the
    agent's ``trust_domains``, and writes ``quarantined: true`` into the
    SKILL.md frontmatter for any skill whose domain falls outside scope.

    At trust level 4+ (specialist), no domain restriction applies and no
    skills are quarantined.

    Returns a list of ``SkillQuarantined`` events for each newly quarantined skill.
    """
    events: list[SkillQuarantined] = []
    if not learned_dir.exists():
        return events

    for skill_dir in learned_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_path = skill_dir / "SKILL.md"
        if not skill_path.exists():
            continue

        try:
            post = frontmatter.load(str(skill_path))
        except Exception:
            logger.warning("Failed to parse %s for quarantine check", skill_path)
            continue

        # Already quarantined -- skip.
        if post.metadata.get("quarantined", False):
            continue

        skill_domain = post.metadata.get("domain", "")

        # Specialists (level 4+) have no domain restriction.
        if trust_level >= 4:
            continue

        # If the skill's domain is not in the agent's trust domains, quarantine.
        if skill_domain and trust_domains and skill_domain not in trust_domains:
            post.metadata["quarantined"] = True
            post.metadata["quarantine_reason"] = (
                f"trust demoted: domain '{skill_domain}' not in agent scope"
            )
            skill_path.write_text(frontmatter.dumps(post), encoding="utf-8")
            events.append(SkillQuarantined(
                skill_name=post.metadata.get("name", skill_dir.name),
                reason=f"domain '{skill_domain}' outside trust scope after demotion",
            ))

    return events
