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
│  Layer 4: MCPVirtualServer        │  ◄── Which curated tool subset
│  (CEL group → tool view)         │      does this user see?
└──────────────┬───────────────────┘
               │
               ▼
         MCP Server
```

## Enforcement Tiers

**Tier 1 -- Hard enforcement (Authorino, layers 1-2).** Requests are rejected outright with HTTP 401 (invalid token) or 403 (unauthorized for this server). These fire before the broker sees the request.

**Tier 2 -- Broker intersection enforcement (layers 3+4).** When BOTH the wristband (`x-authorized-tools`) AND the VirtualMCPServer (`x-mcp-virtualserver`) are active, the broker enforces their intersection on `tools/call` -- not just `tools/list`. A tool must appear in both layers to be callable. Either layer alone only filters `tools/list` discovery; both together enforce execution.

**Tier 3 -- OPA Rego defense-in-depth (AuthPolicy).** The AuthPolicy's OPA Rego checks the `x-mcp-toolname` and `x-mcp-servername` headers (injected by the ext_proc on `tools/call` requests) against the caller's JWT `resource_access` client roles. This catches unauthorized calls at the Authorino level, before the request reaches the broker. It is a defense-in-depth layer that does not depend on the broker's behavior.

## Key Implication for Platform Engineers

All three tiers should be active for a production deployment. Tier 2 (broker intersection) and Tier 3 (OPA Rego) are complementary -- the broker enforces based on wristband + VirtualMCPServer state, while the Rego enforces based on JWT claims. Neither alone is sufficient: the broker cannot enforce without both headers present, and the Rego cannot enforce without the ext_proc injecting `x-mcp-toolname`.

Tool permissions are managed in Keycloak as client roles on bearer-only clients that match MCPServerRegistration names. The OPA Rego reads these from the JWT's `resource_access` claim dynamically -- adding or removing tool permissions only requires Keycloak admin changes, not AuthPolicy edits.

## Further Reading

- `docs/MCP-Ecosystem/05-supported-workflows.md` -- section 5.1.5 for full AuthPolicy configuration
- `docs/MCP-Ecosystem/09-best-practices.md` -- section 9.1 for security best practices
