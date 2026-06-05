"""Typed results and error hierarchy for subagent-tool invocations.

Subagent tools use structured result objects and a hierarchy of error
types to let the LLM and the parent agent distinguish failure modes
and respond appropriately.
"""

from __future__ import annotations

from dataclasses import dataclass


__all__ = [
    "SubagentResult",
    "SubagentError",
    "SubagentTimeoutError",
    "SubagentRemoteError",
    "MaxDelegationDepthError",
    "SubagentCrashedError",
]


@dataclass
class SubagentResult:
    """Outcome of a single subagent invocation, returned by the transport.

    Forwarded to the LLM as JSON; carries the data the parent's loop
    and the server's cost-roll-up plumbing need.

    Attributes:
        agent_name: Name of the invoked subagent.
        content: Final assistant message text from the subagent.
        tokens_used: Token count breakdown:
            ``{"input": <int>, "output": <int>, "cached": <int>}``.
        tool_calls_made: Number of tool calls the subagent made.
        cost_usd: Cumulative cost incurred by the subagent.
        span_id: OTEL span ID, populated when tracing is enabled.
        finish_reason: Copy of the model's finish_reason (default "stop").
    """

    agent_name: str
    content: str
    tokens_used: dict[str, int]
    tool_calls_made: int
    cost_usd: float
    span_id: str | None = None
    finish_reason: str = "stop"


class SubagentError(Exception):
    """Base for all subagent-tool failures.

    Catchable as a single type by the dispatcher; concrete subclasses
    carry mode-specific fields.
    """

    def __init__(self, agent_name: str, message: str) -> None:
        """Initialize the error with agent name and message.

        Args:
            agent_name: The name of the subagent that failed.
            message: A concise description of the failure.
        """
        self.agent_name = agent_name
        super().__init__(f"subagent {agent_name!r}: {message}")


class SubagentTimeoutError(SubagentError):
    """The transport's timeout_seconds elapsed without the subagent returning.

    This is a transient failure — the parent's LLM may retry or replan.
    """

    def __init__(self, agent_name: str, timeout_seconds: float) -> None:
        """Initialize a timeout error.

        Args:
            agent_name: Name of the subagent that timed out.
            timeout_seconds: The timeout duration that was exceeded.
        """
        self.timeout_seconds = timeout_seconds
        super().__init__(
            agent_name,
            f"timed out after {timeout_seconds:.1f}s",
        )


class SubagentRemoteError(SubagentError):
    """The remote endpoint returned a non-success HTTP status or the connection broke.

    This includes network failures (connection reset, timeout on the
    remote side) as well as server errors (5xx status codes).
    """

    def __init__(
        self,
        agent_name: str,
        *,
        status_code: int | None,
        detail: str,
    ) -> None:
        """Initialize a remote error.

        Args:
            agent_name: Name of the subagent endpoint that failed.
            status_code: HTTP status code, or None if the connection
                broke before a response could be obtained.
            detail: Human-readable description of the error.
        """
        self.status_code = status_code
        self.detail = detail
        suffix = f"HTTP {status_code}: " if status_code is not None else ""
        super().__init__(agent_name, f"{suffix}{detail}")


class MaxDelegationDepthError(SubagentError):
    """A delegation chain reached its configured max_depth cap.

    This is a non-recoverable failure for the current delegation path.
    The parent's LLM cannot retry the call without changing the plan.
    """

    def __init__(
        self,
        agent_name: str,
        *,
        depth: int,
        max_depth: int,
    ) -> None:
        """Initialize a max depth error.

        Args:
            agent_name: Name of the subagent whose invocation was denied.
            depth: The current delegation depth when the call was rejected.
            max_depth: The configured maximum depth for the chain.
        """
        self.depth = depth
        self.max_depth = max_depth
        super().__init__(
            agent_name,
            f"depth {depth} exceeds max_depth {max_depth}",
        )


class SubagentCrashedError(SubagentError):
    """An exception escaped the inprocess transport's run loop.

    The framework converts the crash to this structured error so the
    parent's loop can continue cleanly. The original exception is
    preserved for debugging.
    """

    def __init__(self, agent_name: str, *, original: BaseException) -> None:
        """Initialize a crash error.

        Args:
            agent_name: Name of the subagent that crashed.
            original: The exception that escaped the subagent's loop.
        """
        self.original = original
        super().__init__(
            agent_name,
            f"crashed: {type(original).__name__}: {original}",
        )
