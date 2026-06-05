"""WorkflowNode protocol -- the minimal contract any node must satisfy.

Uses ``typing.Protocol`` for structural subtyping. Both BaseNode and
AgentNode satisfy this protocol without inheriting from it.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

T = TypeVar("T")


class WorkflowNode(Protocol[T]):
    """Structural protocol for workflow nodes.

    Any object with a ``name`` attribute and an async ``process`` method
    that accepts and returns state satisfies this protocol.
    """

    name: str

    async def process(self, state: T) -> T: ...
