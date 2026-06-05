"""Workflow orchestration engine for composing agents and nodes into directed graphs."""

from fipsagents.workflow.errors import (
    EdgeResolutionError,
    MaxStepsExceededError,
    NodeNotFoundError,
    StateValidationError,
    WorkflowError,
)
from fipsagents.workflow.state import END, WorkflowState
from fipsagents.workflow.protocol import WorkflowNode
from fipsagents.workflow.node import BaseNode
from fipsagents.workflow.decorator import node  # must come after .node import to avoid module shadowing
from fipsagents.workflow.graph import Graph
from fipsagents.workflow.runner import WorkflowRunner
from fipsagents.workflow.agent_node import AgentNode
from fipsagents.workflow.remote_node import RemoteNode, RemoteNodeError

__all__ = [
    "WorkflowState",
    "END",
    "BaseNode",
    "WorkflowNode",
    "node",
    "Graph",
    "WorkflowRunner",
    "AgentNode",
    "RemoteNode",
    "RemoteNodeError",
    "WorkflowError",
    "NodeNotFoundError",
    "EdgeResolutionError",
    "StateValidationError",
    "MaxStepsExceededError",
]
