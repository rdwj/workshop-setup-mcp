"""Subagent-as-tool integration.

Public API:

Types / errors:
    SubagentResult: Typed outcome of a subagent invocation.
    SubagentError: Base error class for all subagent failures.
    SubagentTimeoutError: Subagent invocation timed out.
    SubagentRemoteError: Remote endpoint error or connection failure.
    MaxDelegationDepthError: Delegation chain exceeded max_depth.
    SubagentCrashedError: Inprocess subagent raised an exception.

Transports:
    SubagentTransport: Abstract base for invocation transports.
    RemoteSubagentTransport: HTTP transport (OpenAI-compatible endpoint).
    InProcessSubagentTransport: Same-process transport (BaseAgent subclass).
"""

from fipsagents.subagents.types import (
    MaxDelegationDepthError,
    SubagentCrashedError,
    SubagentError,
    SubagentRemoteError,
    SubagentResult,
    SubagentTimeoutError,
)
from fipsagents.subagents.transport import (
    InProcessSubagentTransport,
    RemoteSubagentTransport,
    SubagentTransport,
)

__all__ = [
    "SubagentResult",
    "SubagentError",
    "SubagentTimeoutError",
    "SubagentRemoteError",
    "MaxDelegationDepthError",
    "SubagentCrashedError",
    "SubagentTransport",
    "RemoteSubagentTransport",
    "InProcessSubagentTransport",
]
