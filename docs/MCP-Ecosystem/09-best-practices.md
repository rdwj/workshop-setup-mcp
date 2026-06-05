## **9\. Best Practices & Constraints**

### **9.1 Security Considerations and Recommended Patterns**

##### **9.1.1 Design tool subsets for different audiences**

A typical deployment may include multiple MCPVirtualServers, each tailored to a specific user group:

* **Admin tools** — full access to all registered tools across all servers (OpenShift, GitHub, utility tools).  
* **Specialized tools** — tools from a specific server only (e.g., only GitHub tools for a team focused on code review).  
* **Basic tools** — a minimal, safe set of tools for general users (e.g., only read-only utility tools).

Each MCPVirtualServer typically maps to a Keycloak group or role. The gateway-level AuthPolicy (configured in section 5.1.4) can use CEL expressions to route users to the correct MCPVirtualServer based on group membership, realm roles, or other JWT claims.

Users can also create their own MCPVirtualServers to further restrict the tool set exposed to their AI workflows — for example, scoping an agent to only the three tools relevant to a specific task rather than the full set available to the user's group. This is equivalent to filtering tools at the application level, but managed declaratively through the gateway.

##### **9.1.2 MCPVirtualServers are not an access control mechanism**

MCPVirtualServers are a logical filter on `tools/list` responses — they control what users **see**, not what they can **call**. A user or LLM agent that knows a tool name can still attempt to call it directly via `tools/call`, bypassing the MCPVirtualServer filter entirely. Authorization enforcement must be handled separately via AuthPolicy resources.

The full authorization stack operates in layers (see section 5.1.5 for details):

1. **Gateway-level JWT validation** — is the token valid? (401 if not)  
2. **Per-server patternMatching** — is this user allowed to reach this server? (403 if not)  
3. **Wristband tool filtering** — which specific tools can this user call? (broker filters `tools/list` and rejects unauthorized `tools/call`)  
4. **MCPVirtualServer routing** — which curated tool subset does this user see? (broker selects the tool view)

Layers 1 and 2 are hard enforcement (requests are rejected). Layers 3 and 4 are filtering (tools are hidden or restricted). The broker enforces the intersection: a misconfigured MCPVirtualServer that lists unauthorized tools will result in 403 errors when the LLM attempts to call them — degrading the user experience without providing any security benefit.

##### **9.1.3 Connect clients to the Istio gateway service, not the broker**

The MCP Gateway creates two services: the **broker** (`mcp-gateway`) and the **Istio gateway** (`mcp-gateway-<gatewayclass-name>`). Both listen on port 8080 and both respond to the MCP protocol, but only the Istio gateway routes `tools/call` through the ext_proc filter chain where authorization, wristband verification, and MCPVirtualServer selection take effect.

The broker responds to `tools/list` from its aggregated cache, which can give the false impression that everything is working. However, `tools/call` requests sent directly to the broker bypass the ext_proc filter chain entirely — authorization headers are not processed, wristband tokens are not verified, and MCPVirtualServer routing does not take effect.

Always configure MCP clients — agents, the `gen-ai-aa-mcp-servers` ConfigMap for Gen AI Studio, programmatic MCP clients — with the Istio gateway service URL:

```
http://mcp-gateway-<gatewayclass-name>.mcp-system.svc.cluster.local:8080/mcp
```

Not the broker URL:

```
http://mcp-gateway.mcp-system.svc.cluster.local:8080/mcp
```

This distinction is easy to miss because the broker service has the more intuitive name. On Red Hat OpenShift, the GatewayClass is typically named `data-science-gateway-class`, making the correct service `mcp-gateway-data-science-gateway-class`.

### **9.2 Gateway Topology: When to Use Gateway-per-Team**

Section 7 describes two gateway topologies — shared gateway (policy-isolated) and gateway-per-team (namespace-isolated). Neither is inherently "correct"; the right choice depends on the organization's trust model, compliance requirements, and operational maturity.

**Start with a shared gateway for simplicity.** A shared gateway is the simplest deployment: one namespace, one Gateway, one broker. Teams are separated by Keycloak groups and CEL predicates in AuthPolicies. This is appropriate for single-team deployments, early-stage adoption, or organizations where teams trust each other and compliance requirements do not mandate physical separation.

**Graduate to gateway-per-team when isolation requirements appear.** The gateway-per-team topology is recommended when any of the following apply:

* **Multi-team environments with compliance requirements** (SOC2, HIPAA, FedRAMP) where auditors expect namespace-level or network-level separation between teams.  
* **Teams that manage sensitive or mutually untrusted workloads** — a misconfigured AuthPolicy or MCPVirtualServer in the shared model could expose one team's tools or credentials to another.  
* **Blast radius containment** — in the gateway-per-team model, a misconfigured policy affects only the team that owns it. In the shared model, a misconfiguration can affect all teams sharing the namespace.  
* **Independent lifecycle management** — teams that need to upgrade, scale, or reconfigure their gateway independently without coordinating with other teams.

**Why gateway-per-team is the stronger default for production multi-team deployments.** Although the shared gateway is simpler to operate, the gateway-per-team topology provides defense-in-depth that does not rely on AuthPolicy correctness alone. Kubernetes RBAC, NetworkPolicy, and namespace boundaries enforce isolation independently of the policy engine. This makes gateway-per-team the safer choice when multiple teams are involved and the consequences of a policy misconfiguration are non-trivial.

**The transition is straightforward.** Moving from a shared gateway to gateway-per-team does not require changing the ownership model (who owns which CRs) — only the namespace and gateway topology changes. MCP server CRs, AuthPolicies, and MCPVirtualServers are moved into per-team namespaces, and each team gets its own Gateway, broker, and Route. The RHOAI ConfigMap is updated to point each team's OGX instance at the team's gateway URL.

