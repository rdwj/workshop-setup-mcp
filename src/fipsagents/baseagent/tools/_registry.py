"""Two-plane tool system for BaseAgent.

Implements the ``@tool`` decorator, ``ToolRegistry`` for registration and
discovery, schema generation from type hints, and a central dispatch point
that all tool calls flow through (enabling logging, RBAC, and retry hooks).
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import re
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Union, get_args, get_origin

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Visibility = Literal["agent_only", "llm_only", "both"]
_VALID_VISIBILITIES: frozenset[str] = frozenset({"agent_only", "llm_only", "both"})

# Sentinel attribute name stored on decorated functions.
_TOOL_MARKER = "__base_agent_tool__"

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """Represents an inbound tool invocation (from LLM or agent code)."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])


class ToolResult(BaseModel):
    """Outcome of a tool execution."""

    call_id: str
    name: str
    result: str = ""
    error: Optional[str] = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


# ---------------------------------------------------------------------------
# Tool metadata (attached to decorated functions)
# ---------------------------------------------------------------------------


class ToolMeta(BaseModel):
    """Metadata extracted from the ``@tool`` decorator and the function."""

    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    visibility: Visibility
    fn: Any  # the actual callable (sync or async)
    is_async: bool
    parameters: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool | Callable[..., Any] = False

    def matches_plane(self, plane: Visibility) -> bool:
        """Return True if this tool is accessible from *plane*."""
        if self.visibility == "both":
            return True
        return self.visibility == plane


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------


def tool(
    description: str,
    visibility: Visibility,
    *,
    name: str | None = None,
    requires_approval: bool | Callable[..., Any] = False,
) -> Any:
    """Decorator that marks a function as a BaseAgent tool.

    Parameters
    ----------
    description:
        Human-readable description of what the tool does.
    visibility:
        Which plane(s) may invoke this tool.
    name:
        Override the tool name (defaults to the function name).
    requires_approval:
        Whether this tool requires user approval before execution.
        Can be a bool or a callable that evaluates at runtime.

    Usage::

        @tool(description="Search the web", visibility="llm_only")
        async def web_search(query: str) -> str:
            ...
    """
    if visibility not in _VALID_VISIBILITIES:
        raise ValueError(
            f"visibility must be one of {sorted(_VALID_VISIBILITIES)}, "
            f"got {visibility!r}"
        )

    def decorator(fn: Any) -> Any:
        tool_name = name or fn.__name__
        is_async = asyncio.iscoroutinefunction(fn)

        # Build a lightweight parameter spec from type annotations.
        params = _params_from_signature(fn)

        # If the function has a docstring, append it to the description.
        full_desc = description
        if fn.__doc__:
            cleaned = _clean_docstring(fn.__doc__)
            if cleaned and cleaned != description:
                full_desc = f"{description}\n\n{cleaned}"

        meta = ToolMeta(
            name=tool_name,
            description=full_desc,
            visibility=visibility,
            fn=fn,
            is_async=is_async,
            parameters=params,
            requires_approval=requires_approval,
        )
        setattr(fn, _TOOL_MARKER, meta)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Type-hint → JSON schema helpers
# ---------------------------------------------------------------------------

# Maps Python primitive types to JSON Schema type strings.
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _clean_docstring(doc: str) -> str:
    """Trim and dedent a docstring for use as a description."""
    lines = doc.expandtabs().splitlines()
    stripped = [line.strip() for line in lines]
    # Drop leading/trailing blank lines.
    while stripped and not stripped[0]:
        stripped.pop(0)
    while stripped and not stripped[-1]:
        stripped.pop()
    return "\n".join(stripped)


def _type_to_schema(annotation: Any) -> dict[str, Any]:
    """Convert a single Python type annotation to a JSON Schema fragment."""
    # Handle None / NoneType
    if annotation is type(None):
        return {"type": "null"}

    # Pydantic models
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.model_json_schema()

    # Primitive types
    if annotation in _TYPE_MAP:
        return {"type": _TYPE_MAP[annotation]}

    # list / List[X]
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        if args:
            return {"type": "array", "items": _type_to_schema(args[0])}
        return {"type": "array"}

    # dict / Dict[K, V]
    if origin is dict:
        return {"type": "object"}

    # Optional[X] is Union[X, None]
    if origin is Union:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            # Optional[X] — schema is just X (we mark the param non-required)
            return _type_to_schema(non_none[0])
        # General Union — not fully supported, fall back to any-of
        return {}

    # Bare `list` or `dict` without subscript
    if annotation is list:
        return {"type": "array"}
    if annotation is dict:
        return {"type": "object"}

    # Unknown / unannotated — return empty schema (accepts anything)
    return {}


def _is_optional(annotation: Any) -> bool:
    """Return True if *annotation* is Optional[X] (i.e. Union[X, None])."""
    origin = get_origin(annotation)
    if origin is Union:
        return type(None) in get_args(annotation)
    return False


def _params_from_signature(fn: Any) -> dict[str, Any]:
    """Build a JSON-Schema-style ``parameters`` dict from a function's type hints."""
    sig = inspect.signature(fn)
    hints = _get_type_hints_safe(fn)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue

        annotation = hints.get(param_name, inspect.Parameter.empty)

        # Build the property schema.
        if annotation is inspect.Parameter.empty:
            prop: dict[str, Any] = {}
        else:
            prop = _type_to_schema(annotation)

        # Extract per-parameter description from docstring (Google-style).
        doc_desc = _extract_param_doc(fn, param_name)
        if doc_desc:
            prop["description"] = doc_desc

        properties[param_name] = prop

        # Determine whether the parameter is required.
        has_default = param.default is not inspect.Parameter.empty
        optional_type = annotation is not inspect.Parameter.empty and _is_optional(annotation)
        if not has_default and not optional_type:
            required.append(param_name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _get_type_hints_safe(fn: Any) -> dict[str, Any]:
    """Get type hints, falling back gracefully on forward-ref errors."""
    try:
        return inspect.get_annotations(fn, eval_str=True)
    except Exception:
        try:
            return inspect.get_annotations(fn, eval_str=False)
        except Exception:
            return {}


def _extract_param_doc(fn: Any, param_name: str) -> str | None:
    """Extract a parameter description from a Google-style docstring."""
    if not fn.__doc__:
        return None
    lines = fn.__doc__.splitlines()
    in_params_section = False
    pattern = re.compile(
        rf"^\s+{re.escape(param_name)}\s*(?:\([^)]*\))?\s*:\s*(.+)"
    )
    for line in lines:
        stripped = line.strip()
        if stripped.lower() in ("args:", "parameters:", "parameters"):
            in_params_section = True
            continue
        if in_params_section:
            # A new section heading (non-indented or different heading)
            if stripped and not line.startswith(" ") and not line.startswith("\t"):
                in_params_section = False
                continue
            m = pattern.match(line)
            if m:
                return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Central registry for all agent tools — local and MCP-discovered.

    Provides registration, discovery, plane-filtered retrieval, schema
    generation, and the ``execute`` dispatch point.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolMeta] = {}
        self._inspector: Any | None = None
        self._security_mode: str = "enforce"

    def set_inspector(self, inspector: Any, *, mode: str = "enforce") -> None:
        """Configure the pre-execution tool call inspector."""
        self._inspector = inspector
        self._security_mode = mode

    def register(self, tool_fn: Any) -> ToolMeta:
        """Register a ``@tool``-decorated function.

        Raises ``ValueError`` if *tool_fn* is not decorated or the name
        is already taken.
        """
        meta = getattr(tool_fn, _TOOL_MARKER, None)
        if meta is None:
            raise ValueError(
                f"{tool_fn!r} is not decorated with @tool — "
                f"cannot register it in the ToolRegistry"
            )
        if meta.name in self._tools:
            raise ValueError(
                f"A tool named {meta.name!r} is already registered"
            )
        self._tools[meta.name] = meta
        logger.debug("Registered tool %r (visibility=%s)", meta.name, meta.visibility)
        return meta

    def discover(self, tools_dir: str | Path) -> list[ToolMeta]:
        """Import .py files in *tools_dir* and register ``@tool``-decorated functions.

        Returns the list of newly registered ``ToolMeta`` instances.
        """
        tools_path = Path(tools_dir)
        if not tools_path.is_dir():
            logger.warning("Tools directory does not exist: %s", tools_path)
            return []

        discovered: list[ToolMeta] = []
        for py_file in sorted(tools_path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            module = self._import_module(py_file)
            if module is None:
                continue
            for attr_name in dir(module):
                obj = getattr(module, attr_name)
                if callable(obj) and hasattr(obj, _TOOL_MARKER):
                    meta: ToolMeta = getattr(obj, _TOOL_MARKER)
                    if meta.name not in self._tools:
                        self._tools[meta.name] = meta
                        discovered.append(meta)
                        logger.debug(
                            "Discovered tool %r from %s",
                            meta.name,
                            py_file.name,
                        )

        return discovered

    def discover_stock(self, agent: Any) -> list[ToolMeta]:
        """Import stock tool modules from this package and register them.

        Stock tools are framework-provided tools that need the agent
        instance (e.g. ``delegate_to_agent``, ``ask_user``).  Each stock
        tool module exports a ``STOCK_TOOL_SPEC`` constant describing its
        factory and optional registration condition.

        Returns the list of newly registered ``ToolMeta`` instances.
        """
        stock_dir = Path(__file__).parent
        discovered: list[ToolMeta] = []

        for py_file in sorted(stock_dir.glob("*.py")):
            if py_file.name.startswith("_") or py_file.name == "__init__.py":
                continue
            module = self._import_module(py_file)
            if module is None:
                continue
            spec = getattr(module, "STOCK_TOOL_SPEC", None)
            if spec is None:
                continue
            if spec.condition is not None and not spec.condition(agent):
                logger.debug(
                    "Skipping stock tool from %s — condition not met",
                    py_file.name,
                )
                continue
            result = spec.factory(agent)
            tools_to_register = result if isinstance(result, list) else [result]
            for tool_fn in tools_to_register:
                meta = self.register(tool_fn)
                discovered.append(meta)
                logger.debug(
                    "Registered stock tool %r from %s", meta.name, py_file.name
                )

        return discovered

    @staticmethod
    def _import_module(path: Path) -> types.ModuleType | None:
        """Import a Python file as a module, returning None on failure."""
        module_name = f"_agent_tools_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            logger.warning("Cannot create module spec for %s", path)
            return None
        module = importlib.util.module_from_spec(spec)
        # Temporarily add to sys.modules so relative imports inside the tool
        # file can resolve (unlikely but defensive).
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            logger.exception("Failed to import tool module %s", path)
            del sys.modules[module_name]
            return None
        return module

    def get(self, name: str) -> ToolMeta | None:
        """Look up a tool by name.  Returns ``None`` if not found."""
        return self._tools.get(name)

    def get_meta(self, name: str) -> ToolMeta | None:
        """Return the ToolMeta for a registered tool, or None."""
        return self._tools.get(name)

    def get_llm_tools(self) -> list[ToolMeta]:
        """Return tools visible to the LLM (``llm_only`` or ``both``)."""
        return [
            t for t in self._tools.values()
            if t.matches_plane("llm_only")
        ]

    def get_agent_tools(self) -> list[ToolMeta]:
        """Return tools visible to agent code (``agent_only`` or ``both``)."""
        return [
            t for t in self._tools.values()
            if t.matches_plane("agent_only")
        ]

    def get_all(self) -> list[ToolMeta]:
        """Return all registered tools regardless of visibility."""
        return list(self._tools.values())

    def generate_schemas(self) -> list[dict[str, Any]]:
        """Generate OpenAI-compatible tool schemas for LLM-visible tools.

        Returns a list of dicts in the ``{"type": "function", "function": {...}}``
        format expected by the OpenAI chat completions ``tools`` parameter.
        """
        schemas: list[dict[str, Any]] = []
        for meta in self.get_llm_tools():
            schemas.append(_tool_meta_to_schema(meta))
        return schemas

    async def execute(self, tool_name: str, args: dict[str, Any] | None = None) -> ToolResult:
        """Execute a tool by name — the central dispatch point.

        All tool calls flow through here so logging, RBAC, and retry
        hooks can be applied uniformly.
        """
        call_id = uuid.uuid4().hex[:12]
        args = args or {}

        meta = self._tools.get(tool_name)
        if meta is None:
            return ToolResult(
                call_id=call_id,
                name=tool_name,
                error=f"Unknown tool: {tool_name!r}",
            )

        logger.debug("Executing tool %r (call_id=%s)", tool_name, call_id)

        # Pre-execution inspection
        if self._inspector is not None:
            inspection = self._inspector.inspect(tool_name, args)
            if not inspection.is_clean:
                audit_logger = logging.getLogger("fipsagents.security.audit")
                for finding in inspection.findings:
                    audit_logger.warning(
                        "tool_inspection_finding tool=%s call_id=%s category=%s "
                        "severity=%s argument=%s description=%s",
                        tool_name, call_id, finding.category,
                        finding.severity, finding.argument_name,
                        finding.description,
                    )
                if self._security_mode == "enforce":
                    descriptions = "; ".join(
                        f.description for f in inspection.findings
                    )
                    return ToolResult(
                        call_id=call_id,
                        name=tool_name,
                        error=f"Tool call blocked by security inspection: {descriptions}",
                    )
                # observe mode: log but continue execution

        try:
            if meta.is_async:
                raw_result = await meta.fn(**args)
            else:
                # Run sync functions in the default executor to avoid blocking
                # the event loop.
                loop = asyncio.get_running_loop()
                raw_result = await loop.run_in_executor(
                    None, lambda: meta.fn(**args)
                )
            return ToolResult(
                call_id=call_id,
                name=tool_name,
                result=str(raw_result) if raw_result is not None else "",
            )
        except Exception as exc:
            logger.exception("Tool %r failed (call_id=%s)", tool_name, call_id)
            return ToolResult(
                call_id=call_id,
                name=tool_name,
                error=f"{type(exc).__name__}: {exc}",
            )


# ---------------------------------------------------------------------------
# Schema generation helper
# ---------------------------------------------------------------------------


def _tool_meta_to_schema(meta: ToolMeta) -> dict[str, Any]:
    """Convert a ``ToolMeta`` into an OpenAI-compatible function tool schema."""
    # Strip trailing newlines/whitespace from the description.
    desc = meta.description.strip()

    function_def: dict[str, Any] = {
        "name": meta.name,
        "description": desc,
    }

    if meta.parameters:
        function_def["parameters"] = meta.parameters

    return {
        "type": "function",
        "function": function_def,
    }
