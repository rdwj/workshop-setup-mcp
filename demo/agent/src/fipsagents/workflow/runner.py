"""WorkflowRunner -- the execution engine for workflow graphs.

Traverses a :class:`~workflow.graph.Graph`, executing nodes in sequence,
resolving edges (linear, conditional, error), and enforcing step limits.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, TypeVar

from fipsagents.baseagent.config import NodeConfig
from fipsagents.workflow.errors import EdgeResolutionError, MaxStepsExceededError
from fipsagents.workflow.graph import Graph
from fipsagents.workflow.remote_node import RemoteNode
from fipsagents.workflow.state import END

logger = logging.getLogger(__name__)

T = TypeVar("T")


class WorkflowRunner:
    """Execute a workflow graph from an initial state to completion.

    Basic usage::

        runner = WorkflowRunner(graph, max_steps=100)
        result = await runner.start(initial_state)

    With remote nodes (topology from agent.yaml)::

        runner = WorkflowRunner(graph, node_configs=config.nodes)
        result = await runner.start(initial_state)

    For finer control over lifecycle::

        runner = WorkflowRunner(graph)
        await runner.setup()
        try:
            result = await runner.run(initial_state)
        finally:
            await runner.shutdown()
    """

    def __init__(
        self,
        graph: Graph,
        *,
        max_steps: int = 50,
        node_retries: int = 2,
        node_configs: dict[str, NodeConfig] | None = None,
    ) -> None:
        self._graph = graph
        self._max_steps = max_steps
        self._node_retries = node_retries
        self._node_configs = node_configs or {}

    async def setup(self) -> None:
        """Initialise nodes that have a ``setup`` method (e.g. AgentNodes)."""
        for name, node in self._graph.nodes.items():
            setup_fn = getattr(node, "setup", None)
            if callable(setup_fn):
                logger.info("Setting up node %r", name)
                await setup_fn()

    async def shutdown(self) -> None:
        """Tear down nodes that have a ``shutdown`` method."""
        for name, node in self._graph.nodes.items():
            shutdown_fn = getattr(node, "shutdown", None)
            if callable(shutdown_fn):
                try:
                    logger.info("Shutting down node %r", name)
                    await shutdown_fn()
                except Exception:
                    logger.warning(
                        "Error shutting down node %r", name, exc_info=True
                    )

    async def start(self, initial_state: T) -> T:
        """Full lifecycle: setup -> run -> shutdown (with guaranteed cleanup)."""
        try:
            await self.setup()
            return await self.run(initial_state)
        finally:
            await self.shutdown()

    async def run(self, initial_state: T) -> T:
        """Traverse the graph from entry point to END or max_steps.

        Validates the graph before execution. Applies per-node retry
        logic and routes to error edges when retries are exhausted.
        """
        self._graph.validate()

        current_name: str = self._graph.entry_point  # type: ignore[assignment]
        state: Any = initial_state

        for step in range(1, self._max_steps + 1):
            if current_name == END:
                logger.info("Workflow reached END after %d step(s)", step - 1)
                return state

            node = self._graph.nodes[current_name]
            state, status = await self._execute_node(current_name, node, state)

            # Log the transition.
            logger.info(
                "node_transition",
                extra={
                    "step": step,
                    "node_name": current_name,
                    "status": status,
                },
            )

            # If the node failed and was routed to an error edge, status
            # will be "error->routing_to:<target>". Extract the target.
            if status.startswith("error->routing_to:"):
                current_name = status.split(":", 1)[1]
                continue

            # If the node failed with no error edge, the exception was
            # re-raised inside _execute_node -- we won't reach here.

            # Resolve the next node.
            current_name = self._resolve_next(current_name, state)

        raise MaxStepsExceededError(self._max_steps, current_name)

    # -- Internal execution --------------------------------------------------

    async def _execute_node(
        self, name: str, node: Any, state: Any
    ) -> tuple[Any, str]:
        """Run a node's process() with retry logic.

        Returns ``(updated_state, status_string)``.
        """
        last_exc: Exception | None = None

        for attempt in range(1, self._node_retries + 1):
            input_hash = _state_hash(state)
            start = time.monotonic()

            try:
                effective_node = self._effective_node(name, node)
                updated = await effective_node.process(state)
                duration_ms = (time.monotonic() - start) * 1000
                output_hash = _state_hash(updated)

                logger.info(
                    "node_transition",
                    extra={
                        "node_name": name,
                        "input_state_hash": input_hash,
                        "output_state_hash": output_hash,
                        "duration_ms": round(duration_ms, 2),
                        "status": "success",
                    },
                )
                return updated, "success"

            except Exception as exc:
                last_exc = exc
                duration_ms = (time.monotonic() - start) * 1000

                if attempt < self._node_retries:
                    logger.warning(
                        "Node %r failed (attempt %d/%d): %s — retrying",
                        name,
                        attempt,
                        self._node_retries,
                        exc,
                    )
                else:
                    logger.error(
                        "Node %r exhausted retries (%d attempts): %s",
                        name,
                        self._node_retries,
                        exc,
                    )

        # All retries exhausted. Check for error edge.
        assert last_exc is not None  # noqa: S101
        error_target = self._graph.error_edges.get(name)
        if error_target is not None:
            logger.warning(
                "Routing to error edge: %r -> %r", name, error_target
            )
            return state, f"error->routing_to:{error_target}"

        # No error edge -- propagate the exception.
        exc_type = type(last_exc).__name__
        logger.info(
            "node_transition",
            extra={
                "node_name": name,
                "status": f"error:{exc_type}",
            },
        )
        raise last_exc

    def _effective_node(self, name: str, node: Any) -> Any:
        """Return a RemoteNode wrapper if config says remote, else the original node."""
        cfg = self._node_configs.get(name)
        if cfg is not None and cfg.type == "remote":
            return RemoteNode(
                name=name,
                endpoint=cfg.endpoint,  # type: ignore[arg-type]  # validated by NodeConfig
                path=cfg.path,
                timeout=cfg.timeout,
                retries=cfg.retries,
            )
        return node

    def _resolve_next(self, current: str, state: Any) -> str:
        """Determine the next node from edges. Conditional edges take priority."""
        # Check conditional edge first.
        cond_fn = self._graph.conditional_edges.get(current)
        if cond_fn is not None:
            target = cond_fn(state)
            if target != END and target not in self._graph.nodes:
                raise EdgeResolutionError(current, target)
            return target

        # Check linear edge.
        linear_target = self._graph.edges.get(current)
        if linear_target is not None:
            return linear_target

        # No edges -- treat as terminal node.
        return END


def _state_hash(state: Any) -> str:
    """Produce a short hash of serialised state for log correlation."""
    try:
        json_bytes = state.model_dump_json().encode()
    except (AttributeError, Exception):
        json_bytes = repr(state).encode()
    return hashlib.sha256(json_bytes).hexdigest()[:12]
