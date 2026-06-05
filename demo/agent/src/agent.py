"""MCP Gateway Auth Demo Agent.

Token acquisition is handled by scripts/start-with-auth.sh which sets
MCP_AUTH_TOKEN before the agent starts. The agent.yaml MCP server config
reads this env var for the Authorization header.
"""

from __future__ import annotations

from fipsagents.baseagent import BaseAgent, StepResult


class MyAgent(BaseAgent):
    """Agent that uses MCP tools through an authenticated gateway."""

    async def step(self) -> StepResult:
        response = await self.call_model()
        response = await self.run_tool_calls(response)
        return StepResult.done(result=response.content)


if __name__ == "__main__":
    from fipsagents.baseagent import load_config
    from fipsagents.server import OpenAIChatServer

    config = load_config("agent.yaml")
    server = OpenAIChatServer(
        agent_class=MyAgent,
        config_path="agent.yaml",
        title=config.agent.name,
        version=config.agent.version,
    )
    server.run(host=config.server.host, port=config.server.port)
