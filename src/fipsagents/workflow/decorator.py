"""The @node decorator for marking functions as workflow nodes.

Mirrors the @tool pattern from fipsagents.baseagent.tools, which uses a sentinel
attribute to attach metadata to decorated functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, overload

# Sentinel attribute name stored on decorated functions/classes.
_NODE_MARKER = "__workflow_node__"


@dataclass
class NodeMeta:
    """Metadata attached to @node-decorated callables."""

    name: str
    error_handler: str | None


# ---------------------------------------------------------------------------
# @node decorator -- supports all three call forms:
#   @node
#   @node()
#   @node(name="custom", error_handler="fallback")
# ---------------------------------------------------------------------------


@overload
def node(fn: Callable[..., Any]) -> Callable[..., Any]: ...


@overload
def node(
    fn: None = None,
    *,
    name: str | None = None,
    error_handler: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


def node(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    error_handler: str | None = None,
) -> Any:
    """Decorator that marks a function or class as a workflow node.

    Attaches :class:`NodeMeta` as a sentinel attribute for discovery.
    Does not alter runtime behaviour.

    Usage::

        @node
        async def validate(state: MyState) -> MyState: ...

        @node()
        async def transform(state: MyState) -> MyState: ...

        @node(name="custom_name", error_handler="fallback")
        async def risky_step(state: MyState) -> MyState: ...
    """

    def _attach(target: Callable[..., Any]) -> Callable[..., Any]:
        resolved_name = name or getattr(target, "__name__", target.__class__.__name__)
        meta = NodeMeta(name=resolved_name, error_handler=error_handler)
        setattr(target, _NODE_MARKER, meta)
        return target

    # Called as @node (no parentheses) -- fn is the decorated target.
    if fn is not None:
        return _attach(fn)

    # Called as @node() or @node(name=...) -- return the actual decorator.
    return _attach
