## **7\. Namespace Isolation and Multi-Tenancy**

The choice of how many gateways and namespaces back a deployment is orthogonal to the ownership patterns (who owns which CRs). Any deployment pattern described above can be deployed in either topology. This section addresses two independent decisions:

1. **Ownership patterns** (Patterns 6.4.1–6.4.4) — who owns which CRs  
2. **Gateway topology** (this section) — how many gateways and namespaces back the deployment

#### 7.1 Possible Gateway Topologies

Gateway topology is flexible — the platform can support any number of gateways, each scoping a different set of servers. Common topologies include:

* **Single shared gateway.** One gateway for all teams and all servers. Simplest to operate; teams are separated by policy (AuthPolicies, VirtualMCPServers, Keycloak groups). Suitable for single-team deployments or multi-team environments with high trust.  
* **Gateway per team.** Each team gets its own gateway, namespace, and broker. Isolation is physical (Kubernetes RBAC, NetworkPolicy, namespace boundaries) rather than policy-based. Suitable when compliance or blast-radius containment requires hard separation.  
* **Gateway per server class.** Gateways are organized by the type of servers behind them rather than by team — for example, one gateway for platform-provided servers (OpenShift tools, built-in utilities) and another for customer-provided or third-party servers. This separates trust domains based on server provenance rather than consumer identity.  
* **Hybrid.** Combinations of the above — for example, a shared gateway for vetted platform servers plus per-team gateways for each team’s custom servers.

The Gateway API model supports all of these: each Gateway is an independent Envoy instance with its own listeners, broker, and set of registered servers. The choice depends on the organization’s trust model, compliance requirements, and operational preferences.

This document focuses on the first two topologies — **shared gateway** and **gateway per team** — as they represent the two ends of the isolation spectrum and cover the most common deployment patterns. The principles and mechanisms (AuthPolicies, VirtualMCPServers, HTTPRoutes, MCPServerRegistrations) apply equally to hybrid or server-class topologies.

#### 7.2 Component Disposition by Topology

The table below maps every component to its disposition in the two primary topologies, using OpenShift AI naming. “Shared” means one instance serves all teams; “per-team” means each team gets its own instance.

| Component | Shared Gateway | Gateway-per-Team | Notes |
| :---- | :---- | :---- | :---- |
| Cluster-scoped operators |  |  |  |
| MCP Gateway operator (mcp-system) | Shared | Shared | Cluster-scoped controller; watches all namespaces |
| MCP Lifecycle operator | Shared | Shared | Reconciles MCPServer CRs across namespaces |
| Kuadrant / Authorino / Limitador (openshift-operators) | Shared | Shared | Policy engine; AuthPolicy CRs reference it from any namespace |
| Service Mesh / istiod | Shared | Shared | Programs Envoy proxies cluster-wide |
| cert-manager \+ ClusterIssuers | Shared | Shared | Cluster-wide certificate lifecycle |
| Identity & secrets |  |  |  |
| Keycloak instance \+ realm | Shared | Shared | Single OIDC provider; groups/roles distinguish teams |
| Keycloak clients | Shared (confidential \+ public) | Per-team (confidential \+ public) | Confidential client for broker service-to-service auth; public client for end-user token acquisition. Per-server clients only needed if token exchange is used to scope credentials to individual servers. |
| Vault cluster \+ JWT auth method | Shared | Shared | One Vault; per-user secrets at secret/mcp-gateway/{sub} |
| Vault policies / roles | Shared (path-scoped) | Per-team (path-scoped) | Vault policy paths can be scoped per team regardless of topology |
| RHOAI integration |  |  |  |
| DataScienceCluster (redhat-ods) | Shared | Shared | Cluster-singleton; enables KServe, OGX operator, Dashboard |
| ConfigMap gen-ai-aa-mcp-servers | Shared | Per-team | Points OGX at gateway URL |
| OGX / vLLM (model serving) | Per-team | Per-team | Each team has its own inference stack in its own namespace |
| Gen AI Studio (playground) | Shared | Shared | Dashboard UI; users select their project |
| Gateway |  |  |  |
| Gateway CR | Shared | Per-team | One Envoy proxy per Gateway |
| Envoy proxy pod | Shared | Per-team | Auto-created by Istio for each Gateway |
| OpenShift Route (edge TLS) | Shared | Per-team | Exposes the Gateway externally |
| MCPGatewayExtension | Shared | Per-team | Tells mcp-controller to deploy a broker for this Gateway |
| MCP Broker / Router | Shared | Per-team | Aggregates tools/list; routes tools/call |
| Config Secret (broker upstream list) | Shared | Per-team | Auto-managed by mcp-controller |
| EnvoyFilter (ext\_proc) | Shared | Per-team | Injects Router into Envoy |
| MCP servers & registrations |  |  |  |
| MCPServer CRs | All in one namespace | Per-team namespace | Servers live alongside their Gateway in the shared model |
| Deployments, Services | All in one namespace | Per-team namespace | Created by lifecycle operator from MCPServer CRs |
| HTTPRoutes | All in one namespace | Per-team namespace | Route per server; references the team’s Gateway |
| MCPServerRegistrations | All in one namespace | Per-team namespace | Registers server with the broker |
| Policy & tool curation |  |  |  |
| AuthPolicy (gateway-level JWT) | Shared (one) | Per-team | JWT validation \+ group→VirtualMCPServer routing |
| AuthPolicy (per-server / per-tool) | Shared namespace | Per-team namespace | Tool-level ACLs, Vault credential injection |
| VirtualMCPServers | Shared namespace (one per group) | Per-team namespace | Tool views; CEL ternary selects by group in shared model |

#### 7.3 Shared Gateway, Policy-Isolated Teams

![][image16]

One namespace, one Gateway, one Envoy, one broker. All teams’ MCP servers, AuthPolicies, and VirtualMCPServers coexist in the same namespace. Teams are separated by Keycloak groups and CEL predicates in AuthPolicies. A single RHOAI ConfigMap points all OGX instances at the same gateway URL.

What’s shared (beyond operators): The Gateway, Envoy, broker, Route, and namespace itself. The broker’s config Secret aggregates all teams’ servers into one upstream list.

What’s logically partitioned: VirtualMCPServers (one per group/team, selected by CEL ternary in the gateway AuthPolicy), per-server AuthPolicies (CEL predicates check group membership), and Keycloak groups/roles.

When to use: Single-team deployments, early-stage adoption, or multi-team environments where teams trust each other and compliance requirements do not mandate physical separation. This is the simpler topology and a reasonable starting point.

Trade-off: Isolation relies entirely on AuthPolicy correctness. The broker sees all servers. If a VirtualMCPServer or AuthPolicy is misconfigured, a user may see or call tools from another team. See section 9.2 for guidance on when to consider moving to the gateway-per-team topology.

Transition path: Moving to gateway-per-team does not require changing the ownership model (who owns which CRs) — only the namespace and gateway topology changes.

#### 7.4 Gateway-per-Team (Namespace-Isolated)

![][image17]

Each team gets its own namespace containing a Gateway, Envoy proxy, broker, MCP servers, AuthPolicies, and VirtualMCPServers. Isolation is physical — Kubernetes RBAC, NetworkPolicy, and namespace boundaries enforce separation independently of AuthPolicy correctness. A misconfigured AuthPolicy or VirtualMCPServer affects only the team that owns it.

What’s shared: Cluster-scoped operators (MCP Gateway, lifecycle, Kuadrant, Service Mesh, cert-manager), Keycloak, Vault, and RHOAI platform components (DataScienceCluster, Dashboard). These are infrastructure services that don’t hold team-specific tool or policy configuration.

What’s replicated per team: Gateway \+ Envoy \+ Route, MCPGatewayExtension \+ broker, all MCPServer CRs and their downstream resources (Deployments, HTTPRoutes, MCPServerRegistrations), AuthPolicies, VirtualMCPServers, the RHOAI ConfigMap pointing OGX at the team’s gateway URL, and the OGX / vLLM inference stack itself.

When to use: Multi-team environments with compliance requirements (SOC2, HIPAA, FedRAMP), teams that don’t fully trust each other, or any situation where a misconfigured policy must not leak tools or credentials across teams.

Trade-off: More infrastructure to provision per team. On OpenShift AI, each team needs its own namespace with Gateway, Route, broker, servers, and a ConfigMap wiring OGX to the team’s gateway. The platform engineer (or automation via a catalog-driven pattern) must provision this per onboarded team. See section 9.2 for guidance on when this additional overhead is justified.

