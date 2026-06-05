"""Two-plane tool system for BaseAgent.

Implements the ``@tool`` decorator, ``ToolRegistry`` for registration and
discovery, schema generation from type hints, and a central dispatch point
that all tool calls flow through (enabling logging, RBAC, and retry hooks).
"""

from __future__ import annotations

from ._registry import (  # noqa: F401 — re-exported for external consumers
    ToolCall,
    ToolMeta,
    ToolRegistry,
    ToolResult,
    Visibility,
    _TOOL_MARKER,
    _is_optional,
    _params_from_signature,
    _tool_meta_to_schema,
    _type_to_schema,
    tool,
)
from ._stock import StockToolSpec  # noqa: F401

__all__ = [
    "tool",
    "ToolMeta",
    "ToolCall",
    "ToolResult",
    "ToolRegistry",
    "Visibility",
    "StockToolSpec",
]
