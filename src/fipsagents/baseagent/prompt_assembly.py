"""Named-layer prompt assembly with precedence and audit logging."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fipsagents.baseagent.prompts import PromptLoader, PromptNotFoundError
from fipsagents.baseagent.rules import RuleLoader
from fipsagents.baseagent.skills import SkillLoader

logger = logging.getLogger("fipsagents.baseagent.prompt_assembly")


@dataclass(frozen=True)
class PromptLayer:
    name: str
    precedence: int
    content: str
    source: str
    token_estimate: int
    mutability: str
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class PromptAssemblyAudit:
    layers: list[PromptLayer]
    total_tokens: int
    assembly_order: list[str]
    skipped_layers: list[str]
    external_layers: list[str]
    timestamp: str


class PromptAssembler:
    def __init__(
        self,
        *,
        identity_source: str = "identity.md",
        identity_inline: str | None = None,
        identity_enabled: bool = True,
        personality_source: str = "personality.md",
        personality_enabled: bool = False,
        governance_enabled: bool = True,
        capabilities_enabled: bool = True,
        base_dir: Path,
        prompts: PromptLoader,
        rules: RuleLoader,
        skills: SkillLoader,
        system_prompt_name: str = "system",
    ):
        self._identity_source = identity_source
        self._identity_inline = identity_inline
        self._identity_enabled = identity_enabled
        self._personality_source = personality_source
        self._personality_enabled = personality_enabled
        self._governance_enabled = governance_enabled
        self._capabilities_enabled = capabilities_enabled
        self._base_dir = base_dir
        self._prompts = prompts
        self._rules = rules
        self._skills = skills
        self._system_prompt_name = system_prompt_name
        self._last_audit: PromptAssemblyAudit | None = None

    def assemble(self) -> str:
        layers = [
            self._load_identity(),
            self._load_personality(),
            self._load_governance(),
            self._load_capabilities(),
        ]

        active = [ly for ly in layers if not ly.skipped and ly.content]
        result = "\n\n---\n\n".join(ly.content for ly in active)

        self._last_audit = PromptAssemblyAudit(
            layers=layers,
            total_tokens=sum(ly.token_estimate for ly in active),
            assembly_order=[ly.name for ly in active],
            skipped_layers=[ly.name for ly in layers if ly.skipped],
            external_layers=["knowledge", "operational_context", "ephemeral"],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            "prompt_assembly layers=%s skipped=%s total_tokens=%d",
            self._last_audit.assembly_order,
            self._last_audit.skipped_layers,
            self._last_audit.total_tokens,
        )
        for layer in layers:
            logger.debug(
                "  layer=%s precedence=%d tokens=%d source=%s skipped=%s reason=%s",
                layer.name,
                layer.precedence,
                layer.token_estimate,
                layer.source,
                layer.skipped,
                layer.skip_reason,
            )

        return result

    def get_audit(self) -> PromptAssemblyAudit | None:
        return self._last_audit

    def _make_layer(
        self,
        name: str,
        precedence: int,
        content: str,
        source: str,
        *,
        skipped: bool = False,
        skip_reason: str = "",
    ) -> PromptLayer:
        return PromptLayer(
            name=name,
            precedence=precedence,
            content=content,
            source=source,
            token_estimate=len(content) // 4 if content else 0,
            mutability="immutable",
            skipped=skipped,
            skip_reason=skip_reason,
        )

    def _load_identity(self) -> PromptLayer:
        if not self._identity_enabled:
            return self._make_layer(
                "identity", 0, "", "", skipped=True, skip_reason="disabled"
            )

        if self._identity_inline:
            return self._make_layer(
                "identity", 0, self._identity_inline, "inline"
            )

        identity_file = self._base_dir / self._identity_source
        if identity_file.exists():
            content = identity_file.read_text()
            return self._make_layer(
                "identity", 0, content, str(identity_file)
            )

        try:
            prompt = self._prompts.get(self._system_prompt_name)
            content = prompt.render()
            return self._make_layer(
                "identity", 0, content, "prompt_loader"
            )
        except PromptNotFoundError:
            return self._make_layer(
                "identity",
                0,
                "",
                "",
                skipped=True,
                skip_reason="no identity source found",
            )

    def _load_personality(self) -> PromptLayer:
        if not self._personality_enabled:
            return self._make_layer(
                "personality", 1, "", "", skipped=True, skip_reason="disabled"
            )

        personality_file = self._base_dir / self._personality_source
        if personality_file.exists():
            content = personality_file.read_text()
            return self._make_layer(
                "personality", 1, content, str(personality_file)
            )

        return self._make_layer(
            "personality", 1, "", "", skipped=True, skip_reason="file not found"
        )

    def _load_governance(self) -> PromptLayer:
        if not self._governance_enabled:
            return self._make_layer(
                "governance", 2, "", "", skipped=True, skip_reason="disabled"
            )

        content = self._rules.get_combined_content()
        if not content:
            return self._make_layer(
                "governance", 2, "", "", skipped=True, skip_reason="no rules loaded"
            )

        return self._make_layer("governance", 2, content, "rules")

    def _load_capabilities(self) -> PromptLayer:
        if not self._capabilities_enabled:
            return self._make_layer(
                "capabilities", 3, "", "", skipped=True, skip_reason="disabled"
            )

        manifest = self._skills.get_manifest()
        if not manifest:
            return self._make_layer(
                "capabilities",
                3,
                "",
                "",
                skipped=True,
                skip_reason="no skills loaded",
            )

        skill_lines = ["# Available Skills", ""]
        for entry in manifest:
            triggers = ", ".join(entry.triggers) if entry.triggers else "none"
            skill_lines.append(
                f"- **{entry.name}**: {entry.description} (triggers: {triggers})"
            )
        content = "\n".join(skill_lines)

        return self._make_layer("capabilities", 3, content, "skills")
