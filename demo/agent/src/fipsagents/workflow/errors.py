"""Workflow-specific exceptions.

Each exception carries enough context for structured logging and
actionable error messages.
"""

from __future__ import annotations


class WorkflowError(Exception):
    """Base exception for all workflow errors."""


class NodeNotFoundError(WorkflowError):
    """Raised when a referenced node does not exist in the graph."""

    def __init__(self, node_name: str, available: list[str] | None = None) -> None:
        self.node_name = node_name
        self.available = available or []
        available_str = f" Available nodes: {', '.join(self.available)}" if self.available else ""
        super().__init__(f"Node {node_name!r} not found in graph.{available_str}")


class EdgeResolutionError(WorkflowError):
    """Raised when an edge function returns an invalid target."""

    def __init__(self, from_node: str, returned: str) -> None:
        self.from_node = from_node
        self.returned = returned
        super().__init__(
            f"Conditional edge from {from_node!r} returned {returned!r}, "
            f"which is not a registered node or END"
        )


class StateValidationError(WorkflowError):
    """Raised when workflow state fails validation."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"State validation failed: {detail}")


class MaxStepsExceededError(WorkflowError):
    """Raised when the workflow exceeds its maximum step count."""

    def __init__(self, max_steps: int, last_node: str) -> None:
        self.max_steps = max_steps
        self.last_node = last_node
        super().__init__(
            f"Workflow exceeded {max_steps} steps (last node: {last_node!r}). "
            f"Possible infinite loop — check conditional edges."
        )
