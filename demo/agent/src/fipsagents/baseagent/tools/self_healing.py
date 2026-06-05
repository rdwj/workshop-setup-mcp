"""Stock tools for self-healing: learn, suggest, and rollback skills."""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from fipsagents.baseagent.events import (
    SkillEdited,
    SkillLearned,
    SkillProposed,
    SkillRolledBack,
)
from fipsagents.baseagent.tools import tool
from fipsagents.baseagent.tools._stock import StockToolSpec

logger = logging.getLogger(__name__)

_KEBAB_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def make_self_healing_tools(agent: object) -> list:
    """Build the self-healing skill tools for this agent.

    Returns a list of 3 ``@tool``-decorated async functions ready for
    ``ToolRegistry.register``.
    """

    def _get_config():
        cfg = getattr(getattr(agent, "config", None), "self_healing", None)
        if cfg is None:
            raise RuntimeError("Self-healing not configured")
        return cfg

    def _get_base_dir() -> Path:
        return getattr(agent, "_base_dir", None) or Path(".")

    def _emit(event):
        buf = getattr(agent, "_self_healing_events", None)
        if buf is not None:
            buf.append(event)

    @tool(
        description="Create or update a learned skill. Skills take effect next session.",
        visibility="llm_only",
        name="learn_skill",
    )
    async def learn_skill(
        name: str,
        description: str,
        content: str,
        domain: str,
        trigger: str,
    ) -> str:
        """Create or update a learned skill on disk.

        Args:
            name: Kebab-case skill name (e.g. ``summarize-pdf``).
            description: One-line description of what the skill does.
            content: Full Markdown body for the SKILL.md file.
            domain: Trust domain this skill belongs to.
            trigger: Natural-language trigger phrase for activation.

        Returns:
            JSON object with skill_name, version, review_status, domain.
        """
        cfg = _get_config()

        if cfg.trust_level < 1:
            return json.dumps({
                "error": "Insufficient trust level",
                "required": 1,
                "current": cfg.trust_level,
            })

        # Stage-aware gating: proto-agents can only suggest, not learn.
        maturation = getattr(agent, "_maturation_manager", None)
        if maturation is not None:
            stage = maturation.current_stage()
            perms = maturation.get_permissions(stage)
            if not perms.can_create_skills:
                return json.dumps({
                    "error": f"Agent at {stage.value} stage cannot create skills. "
                    "Use suggest_skill instead.",
                })

        if not _KEBAB_RE.match(name):
            return json.dumps({
                "error": "Invalid skill name",
                "detail": "Name must be kebab-case: lowercase letters, digits, hyphens, starting with a letter.",
            })

        if domain not in cfg.trust_domains and cfg.trust_level < 4:
            return json.dumps({
                "error": "Domain not permitted",
                "domain": domain,
                "allowed": cfg.trust_domains,
            })

        review_status = (
            "auto_approved" if cfg.review_policy == "audit_only" else "pending_review"
        )

        base_dir = _get_base_dir()
        learned_dir = Path(cfg.learned_skills_dir)
        if not learned_dir.is_absolute():
            learned_dir = base_dir / learned_dir
        skill_dir = learned_dir / name

        # Enforce max_skills cap for new skills (updates are always allowed).
        if not skill_dir.exists() and learned_dir.exists():
            current_count = sum(1 for p in learned_dir.iterdir() if p.is_dir())
            if current_count >= cfg.max_skills:
                return json.dumps({
                    "error": f"Learned skills cap reached ({current_count}/{cfg.max_skills}). "
                    "Remove or consolidate existing skills before adding new ones.",
                })

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"

        version = 1
        if skill_path.exists():
            try:
                post = frontmatter.load(str(skill_path))
                current_version = post.metadata.get("version", 1)
            except Exception:
                current_version = 1

            # Archive the current version.
            versions_dir = skill_dir / ".versions"
            versions_dir.mkdir(parents=True, exist_ok=True)
            archive_path = versions_dir / f"v{current_version}.md"
            shutil.copy2(str(skill_path), str(archive_path))

            version = current_version + 1

        now = datetime.now(timezone.utc).isoformat()
        skill_content = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"author: agent\n"
            f"domain: {domain}\n"
            f"version: {version}\n"
            f"triggers:\n"
            f"  - {trigger}\n"
            f"created_at: {now}\n"
            f"review_status: {review_status}\n"
            f"---\n\n"
            f"{content}\n"
        )
        skill_path.write_text(skill_content, encoding="utf-8")

        _emit(SkillLearned(
            skill_name=name,
            domain=domain,
            version=version,
            review_status=review_status,
        ))

        if version > 1:
            _emit(SkillEdited(
                skill_name=name,
                from_version=version - 1,
                to_version=version,
            ))

        return json.dumps({
            "skill_name": name,
            "version": version,
            "review_status": review_status,
            "domain": domain,
        })

    @tool(
        description="Suggest a new skill for review without writing to disk.",
        visibility="llm_only",
        name="suggest_skill",
    )
    async def suggest_skill(
        name: str,
        description: str,
        content: str,
        domain: str,
        trigger: str,
    ) -> str:
        """Propose a skill without persisting it.

        Available at any trust level. The suggestion is emitted as an event
        for external review pipelines to pick up.

        Args:
            name: Kebab-case skill name.
            description: One-line description.
            content: Proposed Markdown body (not written to disk).
            domain: Intended trust domain.
            trigger: Natural-language trigger phrase.

        Returns:
            JSON object with skill_name, status, review_status, work_item_id.
        """
        if not _KEBAB_RE.match(name):
            return json.dumps({
                "error": "Invalid skill name",
                "detail": "Name must be kebab-case: lowercase letters, digits, hyphens, starting with a letter.",
            })

        work_item_id = None
        store = getattr(agent, "_work_item_store", None)
        if store is not None:
            import uuid
            from fipsagents.server.work_items import WorkItem, WorkItemStatus
            work_item_id = f"skill-review-{name}-{uuid.uuid4().hex[:8]}"
            try:
                # Extract agent name safely
                agent_name = "unknown"
                if hasattr(agent, "config") and hasattr(agent.config, "agent"):
                    agent_name = agent.config.agent.name

                await store.create(WorkItem(
                    id=work_item_id,
                    title=f"Review proposed skill: {name}",
                    description=(
                        f"Skill proposal from suggest_skill.\n\n"
                        f"**Name**: {name}\n"
                        f"**Domain**: {domain}\n"
                        f"**Trigger**: {trigger}\n\n"
                        f"## Description\n{description}\n\n"
                        f"## Proposed Content\n```\n{content}\n```"
                    ),
                    status=WorkItemStatus.review_pending,
                    created_by=agent_name,
                ))
            except Exception:
                logger.warning("Failed to create review work item for skill %s", name, exc_info=True)
                work_item_id = None

        _emit(SkillProposed(
            skill_name=name,
            description=description,
            content=content,
            domain=domain,
            trigger=trigger,
            work_item_id=work_item_id,
        ))

        return json.dumps({
            "skill_name": name,
            "status": "proposed",
            "review_status": "pending_review",
            "work_item_id": work_item_id,
        })

    @tool(
        description="Rollback a learned skill to a previous version.",
        visibility="llm_only",
        name="rollback_skill",
    )
    async def rollback_skill(
        name: str,
        to_version: int,
        reason: str = "",
    ) -> str:
        """Roll a learned skill back to an archived version.

        Args:
            name: Skill name to rollback.
            to_version: Version number to restore.
            reason: Optional explanation for the rollback.

        Returns:
            JSON object with skill_name, from_version, to_version, new_version.
        """
        cfg = _get_config()

        if cfg.trust_level < 3:
            return json.dumps({
                "error": "Insufficient trust level for rollback",
                "required": 3,
                "current": cfg.trust_level,
            })

        base_dir = _get_base_dir()
        learned_dir = Path(cfg.learned_skills_dir)
        if not learned_dir.is_absolute():
            learned_dir = base_dir / learned_dir

        archive_path = learned_dir / name / ".versions" / f"v{to_version}.md"
        if not archive_path.exists():
            return json.dumps({
                "error": "Version not found",
                "skill_name": name,
                "to_version": to_version,
            })

        skill_path = learned_dir / name / "SKILL.md"
        if not skill_path.exists():
            return json.dumps({
                "error": "Skill not found",
                "skill_name": name,
            })

        # Read current version from frontmatter.
        try:
            post = frontmatter.load(str(skill_path))
            current_version = post.metadata.get("version", 1)
        except Exception:
            current_version = 1

        # Archive current version before replacing.
        versions_dir = learned_dir / name / ".versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        current_archive = versions_dir / f"v{current_version}.md"
        shutil.copy2(str(skill_path), str(current_archive))

        # Copy archived version to SKILL.md and update the version number.
        archived_post = frontmatter.load(str(archive_path))
        new_version = current_version + 1
        archived_post.metadata["version"] = new_version
        skill_path.write_text(frontmatter.dumps(archived_post), encoding="utf-8")

        _emit(SkillRolledBack(
            skill_name=name,
            from_version=current_version,
            to_version=to_version,
            reason=reason,
        ))

        return json.dumps({
            "skill_name": name,
            "from_version": current_version,
            "to_version": to_version,
            "new_version": new_version,
        })

    return [learn_skill, suggest_skill, rollback_skill]


STOCK_TOOL_SPEC = StockToolSpec(
    factory=make_self_healing_tools,
    condition=lambda agent: (
        hasattr(agent, "config")
        and getattr(getattr(agent, "config", None), "self_healing", None) is not None
        and getattr(
            getattr(agent, "config", None), "self_healing", None
        ).enabled
    ),
)
