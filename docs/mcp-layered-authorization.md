# MCP Gateway: Layered Authorization Model

The MCP Gateway enforces access control through a four-layer authorization stack. Each layer serves a distinct purpose, and together they provide defense-in-depth for tool access. Understanding this model is critical for platform engineers configuring secure MCP deployments.

## Authorization Flow

```
  Client Request
       │
       ▼
┌──────────────────────────────────┐
│  Layer 1: JWT Validation         │  ◄── Is the token valid?
│  (Authorino / Keycloak OIDC)     │      401 Unauthorized if not
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────────┐
│  Layer 2: Per-Server Matching    │  ◄── Is this user allowed to
│  (AuthPolicy patternMatching)    │      reach this server?
└──────────────┬───────────────────┘      403 Forbidden if not
               │
               ▼
┌──────────────────────────────────┐
│  Layer 3: Wristband Filtering    │  ◄── Which specific tools can
│  (OPA Rego → signed wristband)   │      this user call?
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────────┐
│  Layer 4: VirtualMCPServer       │  ◄── Which curated tool subset
│  (CEL group → tool view)         │      does this user see?
└──────────────┬───────────────────┘
               │
               ▼
         MCP Server
```

## Hard Enforcement vs Filtering

Layers 1 and 2 are **hard enforcement** -- requests are rejected outright with HTTP 401 (invalid token) or 403 (unauthorized for this server). Layers 3 and 4 are **filtering** -- tools are either hidden from `tools/list` responses or rejected when called via `tools/call`. The broker enforces the intersection of layers 3 and 4: a VirtualMCPServer that lists tools the user isn't authorized for via the wristband will result in 403 errors when the LLM attempts to call them.

## Key Implication for Platform Engineers

VirtualMCPServers control what users **see**, not what they can **call**. They are not an access control mechanism. Authorization enforcement must be handled via AuthPolicy (layers 1-3). A VirtualMCPServer can restrict a user's view to a subset of authorized tools, but it cannot grant access to tools the wristband denies. Any user or LLM agent that knows a tool name can attempt to call it directly, bypassing the VirtualMCPServer filter -- the wristband (layer 3) is the final enforcement point for tool execution.

## Further Reading

- `docs/MCP-Ecosystem/05-supported-workflows.md` -- section 5.1.5 for full AuthPolicy configuration
- `docs/MCP-Ecosystem/09-best-practices.md` -- section 9.1 for security best practices
