"""BaseNode -- minimal node class for workflow graphs.

Handles routing, transformation, validation, and gating logic.
No LLM, no tools, no MCP -- use AgentNode when you need those.
"""

from __future__ import annotations

import logging
from typing import TypeVar

T = TypeVar("T")


class BaseNode:
    """Lightweight workflow node.

    Subclass and override :meth:`process` to implement node logic::

        class Router(BaseNode):
            async def process(self, state: MyState) -> MyState:
                if state.needs_review:
                    state.next_action = "review"
                else:
                    state.next_action = "publish"
                return state

    The ``name`` attribute defaults to the class name but can be
    overridden via the constructor.
    """

    def __init__(self, name: str | None = None) -> None:
        self.name: str = name or self.__class__.__name__
        self.logger = logging.getLogger(f"workflow.node.{self.name}")

    async def process(self, state: T) -> T:
        """Process state and return updated state.

        Subclasses must override this method.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement process(state)"
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
