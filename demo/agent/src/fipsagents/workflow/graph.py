"""Graph -- directed graph definition for workflow orchestration.

Provides a fluent API for building graphs of nodes connected by
linear edges, conditional edges, and error edges. All mutation
methods return ``self`` for method chaining.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fipsagents.workflow.errors import NodeNotFoundError
from fipsagents.workflow.state import END

logger = logging.getLogger(__name__)


class Graph:
    """Definition of a workflow as a directed graph of nodes.

    Build a graph, then hand it to :class:`~workflow.runner.WorkflowRunner`
    for execution::

        graph = (
            Graph(MyState)
            .add_node("fetch", FetchNode())
            .add_node("parse", ParseNode())
            .add_edge("fetch", "parse")
            .add_edge("parse", END)
            .set_entry_point("fetch")
        )
        result = await WorkflowRunner(graph).start(MyState())
    """

    def __init__(self, state_type: type) -> None:
        self.state_type = state_type

        # node_name -> node instance
        self._nodes: dict[str, Any] = {}

        # Linear edges: from_node -> to_node (or END)
        self._edges: dict[str, str] = {}

        # Conditional edges: from_node -> callable(state) -> node_name | END
        self._conditional_edges: dict[str, Callable[..., str]] = {}

        # Error edges: from_node -> error_node
        self._error_edges: dict[str, str] = {}

        self._entry_point: str | None = None

    # -- Node registration ---------------------------------------------------

    def add_node(self, name: str, node: Any) -> Graph:
        """Register a node in the graph.

        The node must have a callable ``process`` attribute. If it has
        a ``name`` attribute, it will be set to *name*.

        Raises ``TypeError`` if the node lacks a callable ``process``.
        Raises ``ValueError`` if *name* is already registered.
        """
        process_attr = getattr(node, "process", None)
        if not callable(process_attr):
            raise TypeError(
                f"Node {name!r} must have a callable 'process' method, "
                f"got {type(node).__name__}"
            )

        if name in self._nodes:
            raise ValueError(f"Node {name!r} is already registered in the graph")

        if hasattr(node, "name"):
            node.name = name

        self._nodes[name] = node
        logger.debug("Added node %r (%s)", name, type(node).__name__)
        return self

    # -- Edge registration ---------------------------------------------------

    def add_edge(self, from_node: str, to_node: str) -> Graph:
        """Add a linear edge from one node to another (or to END).

        Raises ``NodeNotFoundError`` if either node is not registered
        (END is always valid as a target).
        """
        self._require_node(from_node)
        if to_node != END:
            self._require_node(to_node)

        self._edges[from_node] = to_node
        logger.debug("Added edge %r -> %r", from_node, to_node)
        return self

    def add_conditional_edge(
        self, from_node: str, edge_fn: Callable[..., str]
    ) -> Graph:
        """Add a conditional edge whose target is determined at runtime.

        *edge_fn* receives the current state and must return a registered
        node name or :data:`~workflow.state.END`.

        Raises ``NodeNotFoundError`` if *from_node* is not registered.
        """
        self._require_node(from_node)
        self._conditional_edges[from_node] = edge_fn
        logger.debug("Added conditional edge from %r", from_node)
        return self

    def add_error_edge(self, from_node: str, error_node: str) -> Graph:
        """Add a fallback edge used when *from_node* exhausts retries.

        Raises ``NodeNotFoundError`` if either node is not registered.
        """
        self._require_node(from_node)
        self._require_node(error_node)
        self._error_edges[from_node] = error_node
        logger.debug("Added error edge %r -> %r", from_node, error_node)
        return self

    # -- Entry point ---------------------------------------------------------

    def set_entry_point(self, node_name: str) -> Graph:
        """Set the starting node for workflow execution.

        Raises ``NodeNotFoundError`` if the node is not registered.
        """
        self._require_node(node_name)
        self._entry_point = node_name
        logger.debug("Set entry point to %r", node_name)
        return self

    # -- Validation ----------------------------------------------------------

    def validate(self) -> None:
        """Check the graph for structural errors.

        Raises ``ValueError`` if:
        - No entry point is set
        - A node has both a linear edge and a conditional edge
        """
        if self._entry_point is None:
            raise ValueError("No entry point set. Call set_entry_point() before running.")

        # A node cannot have both a linear AND a conditional edge.
        for node_name in self._nodes:
            has_linear = node_name in self._edges
            has_conditional = node_name in self._conditional_edges
            if has_linear and has_conditional:
                raise ValueError(
                    f"Node {node_name!r} has both a linear edge and a "
                    f"conditional edge. Use one or the other."
                )

    # -- Accessors (used by WorkflowRunner) ----------------------------------

    @property
    def entry_point(self) -> str | None:
        return self._entry_point

    @property
    def nodes(self) -> dict[str, Any]:
        return self._nodes

    @property
    def edges(self) -> dict[str, str]:
        return self._edges

    @property
    def conditional_edges(self) -> dict[str, Callable[..., str]]:
        return self._conditional_edges

    @property
    def error_edges(self) -> dict[str, str]:
        return self._error_edges

    # -- Internals -----------------------------------------------------------

    def _require_node(self, name: str) -> None:
        """Raise ``NodeNotFoundError`` if *name* is not a registered node."""
        if name not in self._nodes:
            raise NodeNotFoundError(name, available=list(self._nodes.keys()))
