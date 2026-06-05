"""Pre-execution inspection of tool call arguments.

Scans tool call argument values for secrets, C2 patterns, and prompt
injection indicators.  Returns all findings at once (no fail-fast).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("fipsagents.security.audit")


@dataclass
class InspectionFinding:
    """A single finding from tool argument inspection."""

    category: str  # "secret", "c2_pattern", "prompt_injection"
    description: str  # human-readable
    severity: str  # "high", "medium", "low"
    argument_name: str  # which argument triggered it


@dataclass
class InspectionResult:
    """Result of inspecting a tool call's arguments."""

    tool_name: str
    findings: list[InspectionFinding] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return len(self.findings) == 0


# ---------------------------------------------------------------------------
# Compiled regex patterns
# ---------------------------------------------------------------------------

# Secret detection (same patterns as sandbox CodeGuard)
_SECRET_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("AWS access key ID", "high", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "generic secret assignment",
        "high",
        re.compile(
            r"(?:api[_-]?key|api[_-]?secret|token|secret[_-]?key"
            r"|password|passwd|auth[_-]?token)"
            r"""\s*[:=]\s*['"][A-Za-z0-9+/=_-]{16,}['"]"""
        ),
    ),
    (
        "PEM private key",
        "high",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "high-entropy hex string",
        "medium",
        re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    ),
]

# C2 / exfiltration patterns
_C2_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "base64-encoded payload (long)",
        "medium",
        re.compile(r"[A-Za-z0-9+/]{64,}={0,2}"),
    ),
    (
        "suspicious URL with IP address",
        "medium",
        re.compile(r"https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"),
    ),
    (
        "data URI with base64",
        "medium",
        re.compile(r"data:[^;]+;base64,"),
    ),
]

# Prompt injection heuristics -- instruction-like text in data fields
_INJECTION_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "role override attempt",
        "high",
        re.compile(
            r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above)\s+"
            r"(?:instructions?|rules?|prompts?|guidelines?)",
            re.IGNORECASE,
        ),
    ),
    (
        "system prompt extraction attempt",
        "high",
        re.compile(
            r"(?:show|reveal|output|print|display|repeat)\s+"
            r"(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|rules?)",
            re.IGNORECASE,
        ),
    ),
    (
        "role impersonation",
        "medium",
        re.compile(
            r"you\s+are\s+now\s+|act\s+as\s+|pretend\s+(?:to\s+be|you\s+are)",
            re.IGNORECASE,
        ),
    ),
]


class ToolInspector:
    """Inspects tool call arguments for security concerns.

    Scans all string values (recursively through dicts and lists) in
    tool arguments against secret, C2, and prompt injection patterns.
    """

    def __init__(self, *, min_string_length: int = 16) -> None:
        self._min_string_length = min_string_length

    def inspect(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> InspectionResult:
        """Inspect all arguments of a tool call.

        Returns an InspectionResult with all findings.  An empty findings
        list means the call is clean.
        """
        result = InspectionResult(tool_name=tool_name)

        for arg_name, arg_value in arguments.items():
            self._scan_value(arg_name, arg_value, result)

        return result

    def _scan_value(
        self, arg_name: str, value: Any, result: InspectionResult
    ) -> None:
        """Recursively scan a value for security patterns."""
        if isinstance(value, str):
            self._scan_string(arg_name, value, result)
        elif isinstance(value, dict):
            for k, v in value.items():
                self._scan_value(f"{arg_name}.{k}", v, result)
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                self._scan_value(f"{arg_name}[{i}]", item, result)

    def _scan_string(
        self, arg_name: str, value: str, result: InspectionResult
    ) -> None:
        """Scan a string value against all pattern sets."""
        if len(value) < self._min_string_length:
            return

        # Secret patterns
        for desc, severity, pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                result.findings.append(
                    InspectionFinding(
                        category="secret",
                        description=f"Possible {desc} in argument",
                        severity=severity,
                        argument_name=arg_name,
                    )
                )
                break  # one secret finding per string

        # C2 / exfiltration patterns
        for desc, severity, pattern in _C2_PATTERNS:
            if pattern.search(value):
                result.findings.append(
                    InspectionFinding(
                        category="c2_pattern",
                        description=f"Possible {desc} in argument",
                        severity=severity,
                        argument_name=arg_name,
                    )
                )
                break  # one C2 finding per string

        # Prompt injection patterns
        for desc, severity, pattern in _INJECTION_PATTERNS:
            if pattern.search(value):
                result.findings.append(
                    InspectionFinding(
                        category="prompt_injection",
                        description=f"Possible {desc} in argument",
                        severity=severity,
                        argument_name=arg_name,
                    )
                )
                break  # one injection finding per string
