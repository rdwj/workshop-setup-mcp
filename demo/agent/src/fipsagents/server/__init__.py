"""Opt-in FastAPI server for OpenAI-compatible chat completions.

Requires the ``fipsagents[server]`` extra (FastAPI + uvicorn).

Example usage::

    from fipsagents.server import OpenAIChatServer
    from myagent import MyAgent

    server = OpenAIChatServer(MyAgent, config_path="agent.yaml")

    if __name__ == "__main__":
        server.run()
"""

from .app import OpenAIChatServer
from .models import ChatCompletionRequest, ChatMessage

__all__ = ["OpenAIChatServer", "ChatCompletionRequest", "ChatMessage"]
