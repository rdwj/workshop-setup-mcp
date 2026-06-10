## **4\. MCP Ecosystem Architecture Overview**

### **4.1 Why Standalone MCP Servers Are Not Enough**

Without a managed ecosystem, organizations adopting MCP on Kubernetes face a compounding set of operational, security, and usability challenges:

1. **No central place to discover available tools.** Teams building AI applications have no way to know what MCP servers already exist, what tools they expose, or whether one meets their needs. Discovery relies on word-of-mouth, shared documents, or reading source code — none of which scale.  
2. **No trust signal for available servers.** Even when a team finds an MCP server, there is no vetting or curation process to indicate whether it has been reviewed, tested, or deemed safe for production use. Teams must independently evaluate each server's security posture and reliability.  
3. **Manual, error-prone server deployment.** Deploying an MCP server on OpenShift requires creating a container image, writing a Deployment manifest, configuring Services and networking, setting up health checks, and wiring TLS. Each server requires this from scratch, and mistakes in any step can leave the server unreachable or misconfigured.  
4. **Every server is individually exposed.** Without a gateway, each MCP server must be exposed with its own Route, its own TLS certificate, and its own authentication mechanism. There is no centralized security boundary — the attack surface grows linearly with the number of servers.  
5. **All-or-nothing tool access.** MCP's native `tools/list` returns every tool a server exposes. There is no built-in mechanism to restrict which tools a specific user or group can see or call. Either a user has access to the server and all its tools, or they have access to none.  
6. **Credential sprawl.** MCP servers that call external APIs (SaaS platforms, databases, cloud services) need credentials. Without centralized secret management, API keys end up hardcoded in ConfigMaps, baked into container images, or scattered across environment variables — all difficult to rotate and easy to leak.  
7. **No dynamic credential injection.** Even with a secret store, there is no mechanism to exchange a user's identity token for a backend-specific credential at tool-call time. Servers either use a single shared credential (losing per-user audit trails) or require users to supply their own credentials (leaking infrastructure concerns into the AI workflow).  
8. **Endpoint sprawl for AI clients.** Each MCP server has its own URL. An AI agent or OGX instance that needs tools from multiple servers must be configured with every server's endpoint, manage connections to each, and handle failures independently. This coupling makes it impractical to add, remove, or relocate servers without reconfiguring every client.  
9. **Infrastructure complexity leaks into the AI workflow.** Without abstraction, AI engineers must understand Kubernetes namespaces, Service DNS, TLS certificates, and token acquisition just to call a tool. This creates a steep onboarding curve and couples AI application development to platform-specific knowledge.  
10. **LLMs degrade with too many tools.** Large language models experience reduced accuracy and increased latency when presented with large tool lists. Without a mechanism to curate a focused subset of tools for a specific model or agent, every LLM sees every tool — increasing token consumption, confusing tool selection, and degrading response quality.  
11. **No audit trail.** There is no centralized record of which user invoked which tool, when, through which server, and with what identity claims. In regulated environments (SOC2, HIPAA, FedRAMP), this absence of an auditable access log is a compliance gap.  
12. **Cross-cutting concerns reimplemented per server.** Capabilities like rate limiting, request logging, TLS termination, and protocol enforcement must be built into or configured for each MCP server individually. There is no shared infrastructure layer to apply these policies uniformly.

### **4.2 High-Level Architecture and Component Interactions**

**![][image1]**

### **4.3 Role of Each Component: MCP Catalog, MCP Deployment, MCP Gateway, Gen AI Studio**

| Component | Role |
| :---- | :---- |
| MCP Catalog | Serves as a browsable inventory of available MCP servers that have been vetted and validated. It displays their capabilities, metadata, exposed tools etc and allows users to initiate deployments. |
| MCP Lifecycle Operator | Manages the Kubernetes lifecycle of MCP servers. Watches `MCPServer` custom resources and creates the necessary Deployment and networking resources. Tracks the health and status of the server. |
| MCP Gateway | Federates multiple MCP servers behind a single endpoint. Enforces identity based authorization restrictions on tools. The broker aggregates `tools/list` responses; the router (Envoy ext\_proc) directs `tools/call` requests to the correct backend server. |
| Gen AI Studio | The Playground UI in the RHOAI Dashboard. Provides a chat interface and orchestrates AI model interactions through OGX, which connects to MCP servers either directly or via the gateway. |

### **4.4 End-to-End Flow: Catalog → Deploy → Gateway → Consume**

The following sequence diagram shows the flow of control and user action from server discovery to usage over a single request:

![][image2]

The high level steps are as follows:

1. User browses catalog to discover available servers and pick one to deploy  
2. User requests deployment of that server  
3. Lifecycle operator deploys the server onto the cluster  
4. User registers the deployed MCP server with the gateway  
5. User creates a playground in Gen AI studio  
6. User asks a question in the chat interface  
7. The request progresses through the stack until a response is prepared  
8. User gets the response

### **4.5 How the Ecosystem provides value**

The MCP Ecosystem on RHOAI provides a purpose-built set of components that directly address each of the challenges described in section 4.1. The following maps each gap to the ecosystem capability that closes it:

1. **No central place to discover available tools → MCP Catalog.** The Catalog provides a browsable, searchable inventory of MCP servers with metadata, tool descriptions, and deployment actions. Teams find tools through a single interface rather than ad-hoc channels.  
2. **No trust signal for available servers → Catalog curation and validation.** Servers listed in the MCP Catalog have been vetted and validated. The Catalog serves as a trust boundary — if a server is listed, it has been reviewed for compatibility and baseline safety.  
3. **Manual, error-prone server deployment → MCP Lifecycle Operator \+ MCPServer CRs.** Instead of hand-writing Deployments and Services, the platform engineer (or AI engineer, depending on the ownership pattern) creates a declarative `MCPServer` custom resource. The Lifecycle Operator reconciles it into the necessary Kubernetes objects automatically. The MCP Catalog provides detailed metadata for included servers, which is pre-populated in the UI and further simplifies the deployment UX.  
4. **Configuration drift across environments → Declarative CRs as source of truth.** `MCPServer`, `AuthPolicy`, `VirtualMCPServer`, and `MCPGatewayExtension` are all Kubernetes custom resources. They can be version-controlled in Git, applied through CI/CD pipelines, and reconciled by operators — making drift detectable and correctable.  
5. **Every server individually exposed → MCP Gateway as a single entry point.** The Gateway federates all registered MCP servers behind one HTTPS endpoint. AI clients connect to one URL; the broker and router handle tool discovery and request dispatch internally. Individual servers are never exposed directly.  
6. **All-or-nothing tool access → AuthPolicies with CEL predicates \+ VirtualMCPServers.** AuthPolicies define per-tool or per-server authorization rules using CEL expressions evaluated against JWT claims (groups, roles, subject). VirtualMCPServers further filter `tools/list` responses so users only see the tools relevant to their role. Together, they provide fine-grained, identity-based tool access control.  
7. **Credential sprawl → Vault integration.** Backend credentials (API keys, tokens, service account secrets) are stored in HashiCorp Vault under structured paths (`secret/mcp-gateway/users/{sub}`). No credentials are baked into container images, ConfigMaps, or environment variables.  
8. **No dynamic credential injection → AuthPolicy metadata evaluators with Vault token exchange.** The gateway's AuthPolicy uses a two-stage metadata pipeline: first, it exchanges the user's JWT for a Vault token via the JWT auth method; then, it reads the user-specific secret from Vault and injects it as a request header (`x-user-credential`). The MCP server receives the credential without the user or AI client ever handling it.  
9. **Endpoint sprawl for AI clients → Broker aggregation behind a single gateway URL.** The MCP Broker aggregates `tools/list` responses from all registered servers and the Envoy-based router directs `tools/call` requests to the correct backend. AI clients — whether Gen AI Studio, OGX, or a custom MCP client — connect to one URL and see a unified tool list.  
10. **Infrastructure complexity leaks into the AI workflow → Gen AI Studio and OGX abstraction.** AI engineers interact through the Gen AI Studio Playground UI or OGX's MCP client integration. They authenticate with a token, see their available tools, and invoke them through natural language or programmatic calls. Kubernetes, namespaces, Routes, and policy configuration are invisible to them.  
11. **LLMs degrade with too many tools → VirtualMCPServers for tool curation.** VirtualMCPServers allow platform or AI engineers to define narrow, purpose-specific tool subsets. An LLM agent for code review might see only three tools rather than the full set of thirty available in the namespace — reducing token consumption and improving tool-selection accuracy.  
12. **No audit trail → Gateway-level request logging and Authorino decision logs.** Every tool invocation passes through the Gateway's Envoy proxy and Authorino policy engine, both of which produce structured logs containing the user identity, requested tool, authorization decision, and timestamp. These logs can be forwarded to a centralized logging stack for compliance and forensic analysis.  
13. **Cross-cutting concerns reimplemented per server → Envoy proxy \+ policy engine as shared infrastructure.** TLS termination, rate limiting, request logging, and protocol enforcement are handled by the Gateway's Envoy proxy and Kuadrant policy engine. Individual MCP servers do not need to implement any of these — at the application layer, they receive plain HTTP requests with credentials already injected. All inter-pod communication within the mesh is automatically encrypted via [Istio mutual TLS (mTLS)](https://istio.io/latest/docs/concepts/security/#mutual-tls-authentication), so credentials and request payloads are never transmitted in cleartext over the network, even though the server application itself does not handle TLS. (Note: Service Mesh with sidecar injection or ambient mode is required for automatic encryption between gateway and servers. Without it, manual TLS configuration on each server is possible but operationally burdensome.)

