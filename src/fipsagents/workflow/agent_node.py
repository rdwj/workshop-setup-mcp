"""AgentNode -- bridge between BaseAgent and workflow graphs.

Provides full BaseAgent capabilities (LLM, tools, memory, MCP) inside a
workflow node. Subclass this and implement ``process(state)`` -- do NOT
implement ``step()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from fipsagents.baseagent import BaseAgent, StepResult
from fipsagents.baseagent.config import AgentConfig

T = TypeVar("T")


class AgentNode(BaseAgent):
    """Workflow node with full BaseAgent capabilities.

    Use this when a node needs LLM calls, tool dispatch, memory, or MCP
    connections. For simpler nodes (routing, validation, transformation),
    use :class:`~workflow.node.BaseNode` instead.

    Subclass and implement :meth:`process`::

        class SummaryNode(AgentNode):
            async def process(self, state: MyState) -> MyState:
                response = await self.call_model([
                    {"role": "user", "content": f"Summarise: {state.text}"}
                ])
                state.summary = response.content
                return state

    The ``name`` attribute is used by the workflow graph for node
    identification and structured logging.
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        config_path: str | Path = "agent.yaml",
        config: AgentConfig | None = None,
        base_dir: str | Path | None = None,
    ) -> None:
        self.name: str = name or self.__class__.__name__
        super().__init__(config_path=config_path, config=config, base_dir=base_dir)

    async def step(self) -> StepResult:
        """Not used in workflow context.

        AgentNodes participate in workflows via ``process(state)``, not
        the standalone agent loop's ``step()`` method. This implementation
        satisfies BaseAgent's ABC requirement.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} is a workflow AgentNode. "
            f"Use process(state) in a workflow, not step() in a loop."
        )

    async def process(self, state: T) -> T:
        """Process workflow state and return updated state.

        Subclasses must override this method.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement process(state)"
        )
