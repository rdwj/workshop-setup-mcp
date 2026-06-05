"""Skill loader with progressive disclosure for managing context budgets.

Discovers skills from the ``skills/`` directory.  Each subdirectory is one
skill, identified by a ``SKILL.md`` file whose YAML frontmatter carries
metadata (name, description, triggers, etc.) and whose Markdown body holds
the full instructions.

At startup only frontmatter is loaded (~100 tokens per skill).  Full content
is loaded on demand via ``activate()`` so an agent can have dozens of skills
without burning its entire context window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
_REQUIRED_FIELDS = ("name", "description")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SkillError(Exception):
    """Raised when a skill cannot be loaded or is misconfigured."""


class SkillNotFoundError(SkillError):
    """Raised when a requested skill does not exist."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Skill:
    """A single agent skill with progressive-disclosure loading.

    After initial discovery only ``name``, ``description``, and other
    frontmatter fields are populated.  ``content`` remains ``None`` until
    the skill is explicitly activated.
    """

    name: str
    description: str
    version: str | None = None
    triggers: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)

    # Full Markdown body — None until activated.
    content: str | None = None
    activated: bool = False

    # Whether this skill was loaded from learned_skills (vs bundled).
    learned: bool = False

    # Path to SKILL.md so we can load content later.
    source_path: Path | None = field(default=None, repr=False)


@dataclass
class SkillManifestEntry:
    """Lightweight summary of a skill for context injection into the LLM."""

    name: str
    description: str
    triggers: list[str]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class SkillLoader:
    """Discovers and lazily loads skills from a directory tree.

    Usage::

        loader = SkillLoader()
        loader.load_all("skills/")
        manifest = loader.get_manifest()   # summaries for LLM context
        skill = loader.get("my-skill")     # auto-activates if needed
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    # -- Discovery -----------------------------------------------------------

    def load_all(self, skills_dir: str | Path) -> list[Skill]:
        """Discover skill directories and load frontmatter only.

        Each immediate subdirectory of *skills_dir* that contains a
        ``SKILL.md`` file is treated as a skill.  Only the YAML frontmatter
        is parsed — the Markdown body is deferred until activation.

        Parameters
        ----------
        skills_dir:
            Root directory containing skill subdirectories.

        Returns
        -------
        list[Skill]:
            All discovered skills (frontmatter only, content is ``None``).

        Raises
        ------
        SkillError:
            If a ``SKILL.md`` file is missing required frontmatter fields.
        """
        root = Path(skills_dir)
        if not root.is_dir():
            logger.debug("Skills directory does not exist: %s", root)
            return []

        self._skills.clear()

        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            skill_file = child / SKILL_FILENAME
            if not skill_file.exists():
                logger.debug(
                    "Skipping %s — no %s found", child.name, SKILL_FILENAME
                )
                continue
            skill = _load_frontmatter(skill_file)
            self._skills[skill.name] = skill
            logger.debug("Discovered skill: %s", skill.name)

        logger.info("Loaded %d skill stub(s)", len(self._skills))
        return list(self._skills.values())

    def load_learned(self, learned_dir: str | Path) -> list[str]:
        """Load learned skills from a directory, skipping name conflicts.

        Same discovery logic as :meth:`load_all` but sets ``learned=True``
        on each skill.  If a learned skill has the same name as a bundled
        skill already in the registry, the bundled version takes precedence
        and the learned skill is skipped with a warning.

        Parameters
        ----------
        learned_dir:
            Root directory containing learned skill subdirectories.

        Returns
        -------
        list[str]:
            Names of successfully loaded learned skills.
        """
        root = Path(learned_dir)
        if not root.is_dir():
            logger.debug("Learned skills directory does not exist: %s", root)
            return []

        loaded: list[str] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            skill_file = child / SKILL_FILENAME
            if not skill_file.exists():
                continue
            try:
                skill = _load_frontmatter(skill_file)
            except SkillError:
                logger.warning(
                    "Skipping invalid learned skill in %s", child.name,
                    exc_info=True,
                )
                continue

            # Skip quarantined skills.
            try:
                post = frontmatter.load(str(skill_file))
                if post.metadata.get("quarantined", False):
                    logger.info(
                        "Skipping quarantined learned skill: %s", skill.name,
                    )
                    continue
            except Exception:
                pass  # frontmatter parse errors already handled above

            if skill.name in self._skills:
                logger.warning(
                    "Learned skill %r conflicts with bundled skill — skipping "
                    "(bundled takes precedence)",
                    skill.name,
                )
                continue

            skill.learned = True
            self._skills[skill.name] = skill
            loaded.append(skill.name)
            logger.debug("Loaded learned skill: %s", skill.name)

        if loaded:
            logger.info("Loaded %d learned skill(s)", len(loaded))
        return loaded

    # -- Manifest ------------------------------------------------------------

    def get_manifest(self) -> list[SkillManifestEntry]:
        """Return lightweight summaries suitable for LLM context injection.

        This never triggers activation — it only uses frontmatter data that
        was already loaded during ``load_all()``.  Learned skills are tagged
        with ``[learned]`` in their description.
        """
        entries = []
        for s in self._skills.values():
            desc = s.description
            if s.learned:
                desc = f"[learned] {desc}"
            entries.append(SkillManifestEntry(
                name=s.name,
                description=desc,
                triggers=list(s.triggers),
            ))
        return entries

    # -- Activation ----------------------------------------------------------

    def activate(self, name: str) -> Skill:
        """Load the full Markdown content of a skill.

        Parameters
        ----------
        name:
            The skill name (as declared in its frontmatter).

        Returns
        -------
        Skill:
            The skill with ``content`` populated and ``activated`` set.

        Raises
        ------
        SkillNotFoundError:
            If no skill with *name* has been discovered.
        SkillError:
            If the skill file cannot be read.
        """
        skill = self._resolve(name)

        if skill.activated:
            return skill

        if skill.source_path is None:
            raise SkillError(
                f"Skill '{name}' has no path — was it loaded correctly?"
            )

        try:
            post = frontmatter.load(str(skill.source_path))
        except Exception as exc:
            raise SkillError(
                f"Failed to read full content of skill '{name}' "
                f"from {skill.source_path}: {exc}"
            ) from exc

        skill.content = post.content
        skill.activated = True
        logger.info("Activated skill: %s", name)
        return skill

    def deactivate(self, name: str) -> None:
        """Deactivate a skill, clearing its content to free context budget.

        Parameters
        ----------
        name:
            The skill name (as declared in its frontmatter).

        Raises
        ------
        SkillNotFoundError:
            If no skill with *name* has been discovered.
        """
        skill = self._resolve(name)
        skill.activated = False
        skill.content = None
        logger.info("Deactivated skill: %s", name)

    # -- Access --------------------------------------------------------------

    def get(self, name: str) -> Skill:
        """Return a skill by name, auto-activating if not yet loaded.

        Parameters
        ----------
        name:
            The skill name.

        Returns
        -------
        Skill:
            The skill with full content available.

        Raises
        ------
        SkillNotFoundError:
            If no skill with *name* has been discovered.
        """
        skill = self._resolve(name)
        if not skill.activated:
            self.activate(name)
        return skill

    def list_skills(self) -> list[str]:
        """Return the names of all discovered skills."""
        return list(self._skills.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __len__(self) -> int:
        return len(self._skills)

    # -- Internals -----------------------------------------------------------

    def _resolve(self, name: str) -> Skill:
        """Look up a skill by name or raise ``SkillNotFoundError``."""
        try:
            return self._skills[name]
        except KeyError:
            available = ", ".join(sorted(self._skills)) or "(none)"
            raise SkillNotFoundError(
                f"Unknown skill '{name}'. Available skills: {available}"
            ) from None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_frontmatter(path: Path) -> Skill:
    """Parse YAML frontmatter from a ``SKILL.md`` file.

    Only the frontmatter is processed; the Markdown body is intentionally
    ignored to keep context costs low during discovery.

    Raises
    ------
    SkillError:
        If the file cannot be parsed or required fields are missing.
    """
    try:
        post = frontmatter.load(str(path))
    except Exception as exc:
        raise SkillError(
            f"Failed to parse {path}: {exc}"
        ) from exc

    metadata: dict[str, Any] = dict(post.metadata)

    missing = [f for f in _REQUIRED_FIELDS if f not in metadata]
    if missing:
        raise SkillError(
            f"{path} is missing required frontmatter field(s): "
            f"{', '.join(missing)}"
        )

    return Skill(
        name=metadata["name"],
        description=metadata["description"],
        version=metadata.get("version"),
        triggers=_as_list(metadata.get("triggers", [])),
        dependencies=_as_list(metadata.get("dependencies", [])),
        parameters=metadata.get("parameters", {}),
        content=None,
        activated=False,
        source_path=path,
    )


def _as_list(value: Any) -> list:
    """Coerce a value to a list, wrapping scalars."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
