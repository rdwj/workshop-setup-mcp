"""Prompt loader for BaseAgent.

Discovers Markdown prompt files with YAML frontmatter from a configurable
directory, parses their metadata and variable declarations, and renders
templates with ``{variable_name}`` substitution.

Prompt format (matches architecture.md)::

    ---
    name: summarize
    description: Summarize a document for the user
    model: default
    temperature: 0.3
    variables:
      - name: document
        required: true
      - name: max_length
        default: "500 words"
    ---

    Summarize the following document in {max_length} or less.

    ## Document

    {document}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PromptError(Exception):
    """Base exception for prompt loading and rendering failures."""


class PromptNotFoundError(PromptError):
    """Raised when a requested prompt name does not exist."""


class PromptVariableError(PromptError):
    """Raised when required variables are missing during render."""


class PromptParseError(PromptError):
    """Raised when a prompt file has malformed or invalid frontmatter."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VariableDefinition:
    """A single declared template variable.

    The ``type`` field is advisory documentation only -- it is **not**
    enforced at runtime.  It exists so prompt authors and tooling can
    communicate the expected kind of value (e.g. ``"string"``,
    ``"integer"``, ``"json"``), but the rendering engine treats all
    variable values as strings regardless.
    """

    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    default: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise PromptParseError("Variable definition must have a non-empty 'name'")


@dataclass(frozen=True)
class PromptParameters:
    """Model parameters embedded in prompt frontmatter."""

    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    def as_kwargs(self) -> dict[str, Any]:
        """Return only the parameters that were explicitly set."""
        result: dict[str, Any] = {}
        if self.model is not None:
            result["model"] = self.model
        if self.temperature is not None:
            result["temperature"] = self.temperature
        if self.max_tokens is not None:
            result["max_tokens"] = self.max_tokens
        return result


@dataclass(frozen=True)
class Prompt:
    """A loaded prompt template with metadata and rendering capability."""

    name: str
    description: str
    variables: tuple[VariableDefinition, ...]
    parameters: PromptParameters
    raw_content: str
    source_path: Path | None = None

    def render(self, **variables: str) -> str:
        """Render the prompt template with the given variables.

        Validates that all required variables (those without defaults) are
        supplied.  Extra variables that don't appear in the template are
        silently ignored.

        Raises
        ------
        PromptVariableError
            If any required variable is missing.
        """
        # Build the effective variable mapping: defaults first, then overrides.
        effective: dict[str, str] = {}
        for var in self.variables:
            if var.default is not None:
                effective[var.name] = var.default

        effective.update(variables)

        # Check required variables are present.
        missing = [
            v.name for v in self.variables
            if v.required and v.name not in effective
        ]
        if missing:
            raise PromptVariableError(
                f"Prompt '{self.name}' requires variables that were not provided: "
                f"{', '.join(sorted(missing))}"
            )

        # Substitute using str.format_map with a permissive mapping so that
        # stray braces (e.g. in Markdown code blocks) don't cause KeyError.
        declared = {v.name for v in self.variables}
        return self.raw_content.format_map(_PermissiveMap(effective, declared))


class _PermissiveMap(dict):
    """A dict subclass that returns ``{key}`` for missing keys.

    This prevents ``str.format_map`` from raising ``KeyError`` on brace
    pairs that aren't declared variables (common in Markdown with code
    fences or JSON examples).
    """

    def __init__(self, mapping: dict[str, str], declared_names: set[str] | None = None):
        super().__init__(mapping)
        self._declared_names = declared_names or set(mapping.keys())

    def __missing__(self, key: str) -> str:
        if key not in self._declared_names:
            logger.warning(
                "Template references undeclared variable '%s' — "
                "returning placeholder unchanged",
                key,
            )
        return "{" + key + "}"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_variable(raw: Any, prompt_name: str, index: int) -> VariableDefinition:
    """Parse a single variable entry from the frontmatter ``variables`` list."""
    if isinstance(raw, str):
        # Shorthand: just a variable name, required by default
        return VariableDefinition(name=raw)

    if not isinstance(raw, dict):
        raise PromptParseError(
            f"Prompt '{prompt_name}': variable at index {index} must be "
            f"a string or mapping, got {type(raw).__name__}"
        )

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise PromptParseError(
            f"Prompt '{prompt_name}': variable at index {index} "
            f"must have a string 'name' field"
        )

    has_default = "default" in raw
    default_val = str(raw["default"]) if has_default else None

    # If a default is provided, the variable is not required unless
    # explicitly marked as such.
    required = raw.get("required", not has_default)

    return VariableDefinition(
        name=name,
        type=raw.get("type", "string"),
        description=raw.get("description", ""),
        required=required,
        default=default_val,
    )


def _parse_parameters(meta: dict[str, Any]) -> PromptParameters:
    """Extract model parameters from frontmatter metadata.

    Supports both top-level keys (``model``, ``temperature``, ``max_tokens``)
    matching the architecture doc format, and a grouped ``parameters`` dict.
    Top-level keys take precedence.
    """
    params = meta.get("parameters", {})
    if not isinstance(params, dict):
        params = {}

    return PromptParameters(
        model=meta.get("model", params.get("model")),
        temperature=meta.get("temperature", params.get("temperature")),
        max_tokens=meta.get("max_tokens", params.get("max_tokens")),
    )


def _parse_prompt_file(path: Path) -> Prompt:
    """Parse a single prompt Markdown file.

    Raises
    ------
    PromptParseError
        When the file cannot be read or its frontmatter is invalid.
    """
    try:
        post = frontmatter.load(str(path))
    except Exception as exc:
        raise PromptParseError(
            f"Failed to parse prompt file '{path}': {exc}"
        ) from exc

    meta: dict[str, Any] = dict(post.metadata)
    body: str = post.content

    # Name: from frontmatter, falling back to filename stem.
    name = meta.get("name")
    if not name or not isinstance(name, str):
        name = path.stem

    description = meta.get("description", "")
    if not isinstance(description, str):
        description = str(description)

    # Variables
    raw_vars = meta.get("variables", [])
    if not isinstance(raw_vars, list):
        raise PromptParseError(
            f"Prompt '{name}': 'variables' must be a list, "
            f"got {type(raw_vars).__name__}"
        )
    variables = tuple(
        _parse_variable(rv, name, i) for i, rv in enumerate(raw_vars)
    )

    parameters = _parse_parameters(meta)

    return Prompt(
        name=name,
        description=description,
        variables=variables,
        parameters=parameters,
        raw_content=body,
        source_path=path,
    )


# ---------------------------------------------------------------------------
# PromptLoader
# ---------------------------------------------------------------------------


class PromptLoader:
    """Discovers and loads prompt templates from a directory.

    Usage::

        loader = PromptLoader()
        loader.load_all("prompts/")

        text = loader.render("summarize", document="...", max_length="200 words")
        prompt = loader.get("summarize")
    """

    def __init__(self) -> None:
        self._prompts: dict[str, Prompt] = {}

    @property
    def names(self) -> list[str]:
        """Sorted list of loaded prompt names."""
        return sorted(self._prompts)

    def load_all(self, prompts_dir: str | Path) -> list[Prompt]:
        """Discover and load all ``.md`` files from *prompts_dir*.

        Previously loaded prompts are cleared.  Returns the list of
        successfully loaded prompts.

        Individual files that fail to parse are logged as warnings and
        skipped.  If *all* files fail, ``PromptParseError`` is raised.

        Raises
        ------
        PromptError
            If the directory does not exist.
        PromptParseError
            If every prompt file in the directory is malformed.
        """
        dirpath = Path(prompts_dir)
        if not dirpath.is_dir():
            raise PromptError(
                f"Prompts directory does not exist: {dirpath.resolve()}"
            )

        self._prompts.clear()
        loaded: list[Prompt] = []
        errors: list[tuple[Path, PromptParseError]] = []

        for md_file in sorted(dirpath.glob("*.md")):
            try:
                prompt = _parse_prompt_file(md_file)
            except PromptParseError as exc:
                logger.warning("Skipping malformed prompt file '%s': %s", md_file, exc)
                errors.append((md_file, exc))
                continue
            if prompt.name in self._prompts:
                logger.warning(
                    "Duplicate prompt name '%s' — file '%s' overwrites '%s'",
                    prompt.name,
                    md_file,
                    self._prompts[prompt.name].source_path,
                )
            self._prompts[prompt.name] = prompt
            loaded.append(prompt)
            logger.debug("Loaded prompt '%s' from %s", prompt.name, md_file)

        if errors and not loaded:
            raise PromptParseError(
                f"All {len(errors)} prompt file(s) in {dirpath} failed to parse"
            )

        logger.info("Loaded %d prompt(s) from %s", len(loaded), dirpath)
        return loaded

    def load_file(self, path: str | Path) -> Prompt:
        """Load a single prompt file and register it.

        Useful for testing or adding prompts outside the standard directory.
        """
        prompt = _parse_prompt_file(Path(path))
        self._prompts[prompt.name] = prompt
        return prompt

    def get(self, name: str) -> Prompt:
        """Return the prompt registered under *name*.

        Raises
        ------
        PromptNotFoundError
            If no prompt with that name has been loaded.
        """
        try:
            return self._prompts[name]
        except KeyError:
            available = ", ".join(sorted(self._prompts)) or "(none)"
            raise PromptNotFoundError(
                f"No prompt named '{name}'. Available prompts: {available}"
            ) from None

    def render(self, name: str, **variables: str) -> str:
        """Look up the prompt by *name* and render it with *variables*.

        Combines ``get()`` and ``Prompt.render()`` for convenience.

        Raises
        ------
        PromptNotFoundError
            If the prompt name is unknown.
        PromptVariableError
            If required variables are missing.
        """
        return self.get(name).render(**variables)

    def list_prompts(self) -> list[dict[str, Any]]:
        """Return metadata for all loaded prompts (for BaseAgent.list_prompts)."""
        return [
            {
                "name": p.name,
                "description": p.description,
                "variables": [
                    {
                        "name": v.name,
                        "type": v.type,
                        "description": v.description,
                        "required": v.required,
                        "default": v.default,
                    }
                    for v in p.variables
                ],
                "parameters": p.parameters.as_kwargs(),
            }
            for p in sorted(self._prompts.values(), key=lambda p: p.name)
        ]
