## **6\. Personas & Responsibilities**

### **6.1 Platform Engineer / AI Ops**

The platform engineer is responsible for the provisioning and management of the MCP infrastructure. These responsibilities encompass:

* Infrastructure Provisioning: This includes the creation of namespaces, the deployment of gateways, and the installation of necessary operators.  
* MCP Server Lifecycle Management: This involves the deployment, configuration, and ongoing maintenance of MCP servers.  
* Identity Management: This requires the configuration of Keycloak realms, clients, users, groups, and roles.  
* Access Control: This necessitates the definition of AuthPolicy resources to facilitate JWT validation, granular per-tool authorization, and secure credential injection.  
* Tool Curation: This is managed through the creation of VirtualMCPServer resources to control the visibility of tools based on user group membership.  
* Monitoring: This involves observing gateway operational health, server registration status, and the enforcement of authentication policies.

The platform engineer's primary workflow involves the use of Kubernetes manifests, Keycloak administrative APIs, and oc/kubectl command-line tools. The role does not require application code development.

### **6.2 AI Engineer**

The AI engineer accesses and utilizes the MCP tools via Gen AI Studio or through programmatic MCP clients. Their typical workflow encompasses the following steps:

* Authentication: Logging in with requisite credentials, typically managed through Keycloak.  
* Tool Discovery: Identifying the available tools, which are filtered based on the user's assigned role or group.  
* Tool Invocation: Executing tools either through natural language commands within the Playground environment or by employing the standard MCP protocol.  
* Abstraction of Infrastructure: The AI engineer is not required to possess knowledge concerning the underlying infrastructure components, such as Kubernetes, Vault, VirtualMCPServers, AuthPolicies, or the specific MCP servers deployed on the cluster.

The AI engineer's interaction is exclusively governed by the standard MCP protocol. All complexities related to the infrastructure are effectively abstracted and managed by the platform engineer's configuration.

### **6.3 How the Personas connect**

The platform engineer's declarative configuration creates a self-service experience for the AI engineer. The gateway layer bridges the two:

The following table illustrates how platform configuration translates into a seamless experience for the AI Engineer within the RHOAI environment:

| Platform Engineer Configuration | AI Engineer Experience |
| :---- | :---- |
| Configures Keycloak groups and users. | "I can log in and instantly get an access token." |
| Sets up VirtualMCPServers and the gateway's AuthPolicy. | "I only see the tools that are relevant to my specific role." |
| Implements AuthPolicy with CEL predicates for specific HTTPRoutes. | "I can successfully call my necessary tools; my unauthorized attempts are automatically blocked." |
| Manages Vault secrets and configures metadata evaluators. | "My tools just function without issues—I don't have to deal with managing API keys." |
| Defines the Namespace, Gateway, and MCPGatewayExtension. | "I have a single, unified URL for all my connections in my namespace." |
| Updates the gen-ai-aa-mcp-servers ConfigMap. | "MCP tools automatically appear within the Gen AI Studio interface." |

### **6.4 Responsibility Distribution alternatives**

The personas above describe one specific distribution of responsibilities — the Platform-Managed pattern, where the platform engineer owns everything and the AI engineer is a pure consumer. This is not the only option. Different organizational contexts may call for different splits.

Below we discuss a few possible patterns. Each shifts the boundary between platform engineer and AI engineer ownership. The right choice depends on team maturity, regulatory requirements, and how many AI engineers the platform team needs to support.

#### 6.4.1 Platform-Managed (Centralized Control)

Best Used For: Environments with strict compliance requirements (e.g., SOC2, HIPAA, FedRAMP), initial platform adoption where AI teams are new to the MCP Ecosystem or any scenario requiring centralized control over tool access and credential management.

Key Rationale: This pattern prioritizes security and stability. The Platform Engineer maintains complete control over the "blast radius," including server deployment, tool visibility, and secure credential injection. This prevents AI Engineers from introducing unauthorized tools, escalating privileges, or making authentication errors, making it the most secure starting configuration.

| Resource / Action | Platform Engineer | AI Engineer |
| :---- | :---- | :---- |
| Infrastructure & Governance |  |  |
| Namespace, Gateway, MCPGatewayExtension | Owns | — |
| Keycloak groups and users | Owns | — |
| Vault credential secrets | Owns | — |
| AuthPolicies (Authorization/Credential Injection) | Owns | — |
| Server Deployment & Lifecycle |  |  |
| MCPServer CRs (Deploy Servers) | Owns | — |
| Tool Curation |  |  |
| VirtualMCPServers | Owns | — |
| Usage |  |  |
| Authenticate and get token | — | Owns |
| tools/list, tools/call | — | Owns |

#### 6.4.2 Self-Service Server Registration

This pattern is suited for a Platform Team working with AI/ML teams that are capable of building and operating their own MCP servers.

Best Used For:

* AI teams with the technical expertise to define their necessary tools and write MCPServer Custom Resources (CRs).  
* Environments where the platform team needs to retain control over essential infrastructure components like authentication and networking.

Key Rationale:

* Empowerment and Ownership: The AI team, being closest to the technical needs, takes full ownership of the server lifecycle, from deployment to management.  
* Safety and Control: The Platform Engineer provides essential governance through guardrails (e.g., namespace quotas, network policies, default authentication). The AI Engineer operates strictly within these defined limits.  
* Alignment with OpenShift Model: This pattern mirrors the standard OpenShift philosophy where the platform team manages the cluster infrastructure, and application teams manage their specific workloads (the MCPServer in this case).  
* Explicit Access Control: While deployment is self-service (AI Engineer creates the MCPServer CR), the Platform Engineer maintains control over access to the deployed tools via AuthPolicies and VirtualMCPServers, ensuring security is enforced.

| Resource / Action | Platform Engineer | AI Engineer |
| :---- | :---- | :---- |
| Infrastructure & Governance |  |  |
| Namespace, Gateway, MCPGatewayExtension | Owns | — |
| Keycloak groups and users | Owns | — |
| Vault credential secrets | Owns | — |
| AuthPolicies (Authorization/Credential Injection) | Owns | — |
| Server Deployment & Lifecycle |  |  |
| MCPServer CRs (Deploy Servers) | — | Owns |
| Tool Curation |  |  |
| VirtualMCPServers | Owns (or shared) | May request changes |
| Usage |  |  |
| Authenticate and get token | — | Owns |
| tools/list, tools/call | — | Owns |

#### 6.4.3 AI Engineer Manages Tool Curation for LLMs

Best Used For: Empowering AI engineers to define a limited, purpose-built set of tools for an LLM-powered application or agent without requiring intervention from the Platform Engineer. The AI engineer uses VirtualMCPServers to scope the tool set for specific LLM agents, not to change their security permissions.

Key Rationale: LLMs perform more efficiently and accurately with a focused set of tools. The VirtualMCPServer Custom Resource allows AI engineers to create narrow, application-specific tool views, which limits noise, reduces token usage, and increases tool-calling performance.

| Resource / Action | Platform Engineer | AI Engineer |
| :---- | :---- | :---- |
| Infrastructure & Governance |  |  |
| Namespace, Gateway, MCPGatewayExtension | Owns | — |
| Keycloak groups and users | Owns | — |
| Vault credential secrets | Owns | — |
| AuthPolicies (Authorization/Credential Injection) | Owns | — |
| Server Deployment & Lifecycle |  |  |
| MCPServer CRs (Deploy Servers) | — | Owns |
| Tool Curation |  |  |
| VirtualMCPServers | — | Owns |
| Usage |  |  |
| Authenticate and get token | — | Owns |
| tools/list, tools/call | — | Owns |

VirtualMCPServers are subtractive only — they filter but cannot expand access. See section 9.1 for details on how they interact with AuthPolicies.

#### 6.4.4 AI Engineer as Namespace Admin

Best Used For: Large, autonomous AI/ML platform teams with Kubernetes expertise who require complete control over their MCP environment.

Key Rationale: To provide maximum autonomy for the AI team, allowing them to own their entire Kubernetes namespace (including Gateway, Servers, Authentication, and Credentials), following the established "namespace-as-a-service" model. The Central Platform Team acts as SRE/infrastructure, providing the foundational shared control plane and namespace provisioning.

| Resource / Action | Platform Engineer | AI Engineer |
| :---- | :---- | :---- |
| Infrastructure & Governance |  |  |
| Namespace, Gateway, MCPGatewayExtension | Owns | — |
| Keycloak groups and users | Owns | — |
| Vault credential secrets | — | Owns |
| AuthPolicies (Authorization/Credential Injection) | — | Owns |
| Server Deployment & Lifecycle |  |  |
| MCPServer CRs (Deploy Servers) | — | Owns |
| Tool Curation |  |  |
| VirtualMCPServers | — | Owns |
| Usage |  |  |
| Authenticate and get token | — | Owns |
| tools/list, tools/call | — | Owns |

The key difference is the significant shift in scope:

* The Platform Engineer's scope shrinks to shared infrastructure and initial namespace provisioning.  
* The AI Team must possess higher required expertise, including a deep understanding of AuthPolicies, Vault integration, and VirtualMCPServers.

The Platform Engineer can mitigate this complexity by providing templates or Helm charts to reduce boilerplate configuration for the AI team.

