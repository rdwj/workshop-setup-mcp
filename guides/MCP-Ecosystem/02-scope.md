## **2\. Scope**

### **2.1 In Scope**

This document covers the following topics, validated through hands-on experimentation on OpenShift AI:

* **MCP Ecosystem architecture.** How the MCP Catalog, MCP Lifecycle Operator, MCP Gateway, and Gen AI Studio fit together, the role of each component, and the end-to-end flow from catalog discovery to tool consumption (sections 3–4).  
* **Platform engineer setup workflows.** Operator installation, gateway namespace configuration, Keycloak identity management, gateway-level and per-tool AuthPolicies, Vault credential injection, and wiring OGX / Gen AI Studio to the gateway (section 5.1).  
* **AI engineer usage workflows.** Catalog browsing, MCP server deployment via the UI and `MCPServer` CR, gateway registration (HTTPRoute \+ MCPServerRegistration), VirtualMCPServer creation, Playground interaction with tool calling, and adding custom servers to the catalog (section 5.2).  
* **Personas and responsibility models.** Platform engineer vs. AI engineer responsibilities and four alternative responsibility distribution patterns — centralized, self-service registration, AI engineer tool curation, and namespace admin (section 6).  
* **Namespace isolation and multi-tenancy.** Shared-gateway (policy-isolated) and gateway-per-team (namespace-isolated) topologies, with component disposition tables and trade-off analysis (section 7).  
* **Bringing your own MCP server.** Packaging, validating, preparing catalog metadata, publishing, and deploying custom MCP servers on RHOAI (section 8).  
* **Best practices and constraints.** Security considerations, gateway topology guidance, known constraints and gotchas, and guidance against unsupported patterns (section 9).

### **2.2 Out of Scope**

The following topics are not covered in this document. They may be relevant to production deployments but were not part of the experimentation and validation that informed this guidance:

* **Observability and monitoring.** Integration of MCP Gateway, broker, and server metrics with observability stacks such as Prometheus, Grafana, or MLFlow. This includes gateway request tracing, tool-call latency dashboards, and alerting on server health — all of which would be important for production operations but were not configured during testing.  
* **Distributed inference patterns.** Integration with distributed model serving architectures such as MaaS (Model as a Service) or llm-d. The experiments used a single vLLM InferenceService per namespace; patterns involving model routing, load balancing across inference replicas, or shared model pools are not addressed.  
* **Multi-cluster and federated deployments.** All experimentation was conducted on a single OpenShift cluster. Cross-cluster MCP Gateway federation, multi-cluster service mesh routing, and scenarios where MCP servers in one cluster are consumed by AI workloads in another are not covered.  
* **High availability and disaster recovery.** The deployment used single-replica instances of Vault, Keycloak, the MCP broker, and MCP servers throughout. Production deployments would require HA configurations (e.g., Vault with Raft storage, Keycloak with multiple replicas and an external database, broker failover), as well as backup and restore procedures for Vault unseal keys, Keycloak realm state, and MCPServer custom resources. None of these were tested.  
* **External identity providers beyond Keycloak.** All authentication and authorization was validated using Red Hat Build of Keycloak (RHBK) as the sole OIDC provider. Integration with enterprise identity systems such as Active Directory, LDAP, Okta, Azure AD/Entra ID, or other third-party OIDC providers was not tested. The AuthPolicy and Vault JWT auth configurations documented here assume Keycloak-issued tokens and would require adaptation for other providers.

