"""Permission resolution for tool calls.

``PermissionSource`` implementations decide whether a tool call should
be allowed, denied, or require human confirmation.  ``NullPermissionSource``
(default) allows everything -- fully backward-compatible.

``StaticPermissionSource`` reads declarative rules from agent config.
The full rule grammar is defined in #164; this module provides the
minimal resolve contract.
"""

from __future__ import annotations

import fnmatch
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass
class PermissionDecision:
    """Result of a permission check for a single tool call."""

    action: Literal["allow", "deny", "ask"]
    tool: str
    rule_id: str | None = None
    scope: str | None = None
    reason: str | None = None


@dataclass
class PermissionRule:
    """A single declarative permission rule."""

    id: str
    tool: str  # tool name or "*" for all
    action: Literal["allow", "deny", "ask"]
    scope: str | None = None
    reason: str | None = None


class PermissionSource(ABC):
    """Pluggable permission resolution backend."""

    @abstractmethod
    async def resolve(
        self,
        tool_name: str,
        *,
        scope: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        """Resolve the permission decision for a tool call."""

    async def close(self) -> None:
        """Release resources. Default no-op."""


class NullPermissionSource(PermissionSource):
    """Allow everything -- no permission checks."""

    async def resolve(
        self,
        tool_name: str,
        *,
        scope: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        return PermissionDecision(action="allow", tool=tool_name)


class StaticPermissionSource(PermissionSource):
    """Config-driven permission rules. First match wins."""

    def __init__(
        self,
        rules: list[PermissionRule] | None = None,
        *,
        default_action: Literal["allow", "deny", "ask"] = "allow",
    ) -> None:
        self._rules = list(rules) if rules else []
        self._default_action = default_action

    async def resolve(
        self,
        tool_name: str,
        *,
        scope: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        for rule in self._rules:
            if rule.scope is not None and rule.scope != scope:
                continue
            if fnmatch.fnmatch(tool_name, rule.tool):
                return PermissionDecision(
                    action=rule.action,
                    tool=tool_name,
                    rule_id=rule.id,
                    scope=scope,
                    reason=rule.reason,
                )
        return PermissionDecision(
            action=self._default_action,
            tool=tool_name,
            scope=scope,
        )


def create_permission_source(
    backend: str | None = None,
    *,
    rules: list[dict[str, Any]] | None = None,
    default_action: str = "allow",
) -> PermissionSource:
    """Create a permission source from config values."""
    if backend is None or backend == "null":
        return NullPermissionSource()
    if backend == "static":
        parsed = [
            PermissionRule(
                id=r.get("id", f"rule_{i}"),
                tool=r.get("tool", "*"),
                action=r.get("action", "allow"),
                scope=r.get("scope"),
                reason=r.get("reason"),
            )
            for i, r in enumerate(rules or [])
        ]
        return StaticPermissionSource(rules=parsed, default_action=default_action)
    raise ValueError(f"Unknown permission source backend: {backend!r}")
