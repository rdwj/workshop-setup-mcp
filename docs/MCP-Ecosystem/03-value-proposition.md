## **3\. Value Proposition & Motivation**

### **3.1 What MCP Is and Why It Matters for AI Workflows**

The Model Context Protocol (MCP) is an open standard developed by Anthropic, that provides a uniform interface for AI models to discover and invoke external tools. An MCP server exposes tools — discrete functions that perform actions or retrieve information — over HTTP using the JSON-RPC 2.0 protocol. An MCP client (such as an AI agent runtime) discovers available tools via `tools/list` and calls them via `tools/call`.

MCP decouples tool implementation from AI model integration: While in the past, tool calling has been done via bespoke specs, or by embedding tools into vector databases etc, with MCP we are able to create a translation layer that can make any existing APIs immediately accessible to AI workflows. A team can build an MCP server that wraps a Kubernetes API, a database, a third-party SaaS, or any internal service, and any MCP-compatible AI system can use it without custom integration code.

### **3.2 How MCP Fits Into Agent-Based and Tool-Enabled AI Workflows**

MCP enables a **tool-augmented AI workflow**: AI models can call external tools through MCP servers, giving them the ability to query live systems, take actions, and access information beyond what's in their training data.

MCP also enables agent-based AI patterns where AI models can take action on behalf of users:

1. **User asks a question** — e.g., "What pods are running in the production namespace?"  
2. **The AI model decides it needs external data** — it generates a tool call request  
3. **routing layer routes the tool call through a Gateway** — which handles authentication, authorization, and routing to the correct MCP server  
4. **The MCP server executes the tool** — e.g., queries the Kubernetes API  
5. **The result flows back to the user**

Without MCP it would be significantly more difficult to expose tools to AI workflows in a consistent, scalable way such that models are able to reliably understand which functionalities they have access to and how to most effectively leverage them.

