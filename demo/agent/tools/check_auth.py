"""Tool to verify MCP gateway authentication status."""
from fipsagents.tools import tool


@tool(description="Check if the MCP gateway is accessible and list available tools", visibility="both")
async def check_gateway_auth(agent) -> str:
    """Test connectivity and auth status of the MCP gateway."""
    try:
        mcp_tools = agent.get_tool_schemas()
        mcp_tool_names = [t["function"]["name"] for t in mcp_tools if not t["function"]["name"].startswith("check_")]
        if mcp_tool_names:
            return f"Gateway accessible. {len(mcp_tool_names)} tools available: {', '.join(mcp_tool_names)}"
        return "Gateway accessible but no MCP tools found. The gateway may not have any registered servers."
    except Exception as e:
        return f"Gateway not accessible: {e}. Authentication may be required."
