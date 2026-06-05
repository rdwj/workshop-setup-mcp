"""Rule loader for plain Markdown rule files.

Rules are persistent behavioral constraints injected into the agent's system
context.  Each rule is a plain Markdown file in the ``rules/`` directory --
no frontmatter, no special syntax.  The filename (without ``.md``) is the
rule's identifier.

All rules are loaded eagerly at startup; there is no lazy loading because
rules are always active.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """A single behavioral rule loaded from a Markdown file.

    Attributes
    ----------
    name:
        Identifier derived from the filename (e.g. ``safety`` for
        ``safety.md``).
    content:
        The full Markdown text of the rule file.
    """

    name: str
    content: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RuleLoadError(Exception):
    """Raised when a rule file cannot be read."""


class RuleNotFoundError(RuleLoadError):
    """Raised when a requested rule name does not exist."""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class RuleLoader:
    """Discovers and loads plain Markdown rule files from a directory.

    Usage::

        loader = RuleLoader()
        loader.load_all(Path("rules"))
        rule = loader.get("safety")
        all_rules = loader.get_all()
        combined = loader.get_combined_content()
    """

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}

    def load_all(self, rules_dir: str | Path) -> list[Rule]:
        """Discover and load every ``.md`` file in *rules_dir*.

        Non-``.md`` files are silently ignored.  An empty or non-existent
        directory is not an error -- it simply means no rules are active.

        Parameters
        ----------
        rules_dir:
            Path to the directory containing rule files.

        Returns
        -------
        list[Rule]:
            The rules that were loaded, sorted by name for deterministic
            ordering.

        Raises
        ------
        RuleLoadError:
            If an individual ``.md`` file exists but cannot be read.
        """
        self._rules.clear()
        path = Path(rules_dir)

        if not path.is_dir():
            logger.debug("Rules directory does not exist: %s", path)
            return []

        md_files = sorted(path.glob("*.md"))
        for md_file in md_files:
            name = md_file.stem
            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuleLoadError(
                    f"Cannot read rule file {md_file}: {exc}"
                ) from exc
            self._rules[name] = Rule(name=name, content=content)
            logger.debug("Loaded rule: %s", name)

        logger.info(
            "Loaded %d rule(s) from %s", len(self._rules), path,
        )
        return self.get_all()

    def get(self, name: str) -> Rule:
        """Return a rule by name.

        Raises
        ------
        RuleNotFoundError
            If no rule with that name has been loaded.
        """
        try:
            return self._rules[name]
        except KeyError:
            available = ", ".join(sorted(self._rules)) or "(none)"
            raise RuleNotFoundError(
                f"No rule named '{name}'. Available rules: {available}"
            ) from None

    def get_all(self) -> list[Rule]:
        """Return all loaded rules, sorted by name."""
        return sorted(self._rules.values(), key=lambda r: r.name)

    def get_combined_content(self, separator: str = "\n\n---\n\n") -> str:
        """Concatenate all rule contents with clear separators.

        Each rule is preceded by a Markdown heading with the rule name so
        the LLM can identify which rule is which in the system context.

        Parameters
        ----------
        separator:
            Text inserted between rules.  Defaults to a horizontal rule
            with surrounding blank lines.

        Returns
        -------
        str:
            The combined text, ready for injection into the system prompt.
            Returns an empty string when no rules are loaded.
        """
        rules = self.get_all()
        if not rules:
            return ""
        sections = [
            f"# Rule: {rule.name}\n\n{rule.content}" for rule in rules
        ]
        return separator.join(sections)
