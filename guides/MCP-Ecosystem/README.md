# MCP Ecosystem and Server Deployment Best Practices on RHOAI

**Author:** Jaideep Rao  
**Date:** April 27, 2026  
**Source:** [RHAISTRAT-1149](https://redhat.atlassian.net/browse/RHAISTRAT-1149)

This guide provides comprehensive implementation guidance for the Model Context Protocol (MCP) Ecosystem on Red Hat OpenShift AI, covering discovery, deployment, secure access, and consumption of MCP servers. It is validated against a production-representative deployment on ROSA (OpenShift 4.21.6, RHOAI 3.4.0).

## Document Sections

### 00-preamble.md
Title and metadata for the guide.

### 01-overview.md
Central reference document overview. Explains the MCP Ecosystem components and their integration: MCP Catalog, MCP server deployment, MCP Gateway integration, and Gen AI Studio consumption. Establishes the baseline: validated patterns, known constraints, and integration seams.

### 02-scope.md
What this guide covers and what it doesn't.

#### In Scope
- MCP Ecosystem architecture
- Platform engineer setup workflows
- AI engineer usage workflows
- Personas and responsibility models
- Namespace isolation and multi-tenancy
- Bringing your own MCP server
- Best practices and constraints

#### Out of Scope
- Observability and monitoring
- Distributed inference patterns
- Multi-cluster and federated deployments
- High availability and disaster recovery
- External identity providers beyond Keycloak

### 03-value-proposition.md
Why MCP matters for AI workflows and how the MCP Ecosystem solves operational challenges.

#### What MCP Is and Why It Matters
MCP as a uniform interface for AI models to discover and invoke external tools.

#### How MCP Fits Into Agent-Based Workflows
Tool-augmented AI patterns where models can take action on behalf of users.

### 04-architecture-overview.md
Deep dive into the MCP Ecosystem architecture and component interactions.

#### Why Standalone MCP Servers Are Not Enough
The 12 operational, security, and usability challenges that standalone MCP servers face without an ecosystem.

#### High-Level Architecture and Component Interactions
Visual architecture overview and component interaction diagram.

#### Role of Each Component
MCP Catalog, MCP Lifecycle Operator, MCP Gateway (broker + router), and Gen AI Studio.

#### End-to-End Flow: Catalog → Deploy → Gateway → Consume
Sequence diagram showing the flow from discovery to usage.

#### How the Ecosystem Provides Value
Mapping each gap from standalone servers to the ecosystem capability that closes it.

### 05-supported-workflows.md
**WARNING: This is a large file (19 snippet references). Use grep to find specific subsections.**

Complete setup and usage workflows for platform engineers and AI engineers.

#### 5.1 Setup Workflows (Platform Engineer)
- **5.1.1 Installing and Configuring Operators**
  - Tier 1: Platform Prerequisites (Service Mesh, cert-manager, Connectivity Link, RHOAI)
  - Tier 2: MCP Ecosystem Operators (MCP Gateway, Keycloak, MCP Lifecycle, Vault)
  - Post-Install Activation (Kuadrant CR, DataScienceCluster, Dashboard features)
- **5.1.2 Setting Up the MCP Gateway Namespace**
  - Create gateway namespace
  - Install Gateway via Helm
  - Create OpenShift Route
  - Generate wristband signing keys
  - Wait for broker readiness
- **5.1.3 Configuring Keycloak for Identity Management**
  - Deploy Keycloak
  - Identity federation with OpenShift
  - Import the realm
  - Create groups for tool routing
- **5.1.4 Configuring Gateway-Level Authentication (AuthPolicy)**
  - JWT authentication
  - Wristband signing for per-tool authorization
  - VirtualMCPServer routing
- **5.1.5 Configuring Per-Tool Authorization (CEL Predicates)**
  - Per-server access restriction
  - Header manipulation
  - Per-tool authorization via OPA Rego
  - Vault credential injection
  - Layered authorization model
- **5.1.6 Setting Up Vault for Credential Injection (Optional)**
  - Enable the secrets engine
  - Add Vault-required claims within Keycloak
  - Configure JWT authentication
  - Create a secrets policy
  - Store per-user credentials
  - Secret path organization
  - AuthPolicy wiring for credential injection
  - Reusability
- **5.1.7 Wiring OGX and Gen AI Studio to the Gateway**
  - Register the gateway with the RHOAI Dashboard
  - Prerequisites for model serving
  - Create a Playground in Gen AI Studio

#### 5.2 Usage Workflows (AI Engineer)
- **5.2.1 Discovering MCP Servers via the MCP Catalog**
  - Built-in and curated servers
  - What the Catalog shows
  - Catalog API
  - From discovery to deployment
- **5.2.2 Deploying MCP Servers onto OpenShift**
  - Deploying from the Catalog UI
  - Deploying directly via MCPServer CR
  - Monitoring deployment status
  - Managing deployments
- **5.2.3 Registering MCP Servers with the MCP Gateway**
  - Create an HTTPRoute
  - Create an MCPServerRegistration
  - Servers that require credentials for tool discovery
  - Verification
- **5.2.4 Creating and Using Virtual MCP Servers**
  - Creating a VirtualMCPServer
  - How VirtualMCPServer selection works
  - VirtualMCPServers are subtractive only
- **5.2.5 Consuming MCP Capabilities in Gen AI Studio / Playground**
  - Obtaining an auth token
  - Enabling tool use in the Playground
  - Interacting with tools
- **5.2.6 Bringing Your Own MCP Server**
  - ConfigMap structure
  - Server entry fields
  - Applying and verifying
  - Adding servers to an existing ConfigMap

### 06-personas-responsibilities.md
Defines roles and alternative responsibility distribution patterns.

#### Platform Engineer / AI Ops
Infrastructure provisioning, MCP server lifecycle, identity management, access control, tool curation, monitoring.

#### AI Engineer
Authentication, tool discovery, tool invocation. Infrastructure is abstracted away.

#### How the Personas Connect
Platform configuration creates self-service experience for AI engineers.

#### Responsibility Distribution Alternatives
- **6.4.1 Platform-Managed (Centralized Control)** — best for compliance environments
- **6.4.2 Self-Service Server Registration** — AI teams deploy servers, platform controls access
- **6.4.3 AI Engineer Manages Tool Curation for LLMs** — AI engineers create VirtualMCPServers for focused tool sets
- **6.4.4 AI Engineer as Namespace Admin** — autonomous teams with full control

### 07-namespace-isolation.md
Gateway topologies for single-team and multi-team deployments.

#### Possible Gateway Topologies
Single shared gateway, gateway per team, gateway per server class, hybrid.

#### Component Disposition by Topology
Detailed table mapping every component (operators, identity, RHOAI, gateway, servers, policy) to shared vs. per-team disposition.

#### Shared Gateway, Policy-Isolated Teams
One namespace, one gateway, one broker. Teams separated by Keycloak groups and CEL predicates.

#### Gateway-per-Team (Namespace-Isolated)
Each team gets its own namespace with Gateway, broker, servers, and policies. Physical isolation via Kubernetes RBAC and NetworkPolicy.

### 08-bring-your-own-mcp-server.md
How to package, validate, publish, and deploy custom MCP servers.

#### 8.1 Packaging and Validating the Server
- **8.1.1 Container Packaging** — UBI base images, HTTP transport, bind to 0.0.0.0, credential headers, stable MCP path, health endpoint, stateless design
- **8.1.2 Protocol and Security Verification** — HTTP transport, tool discovery, OCI image, vulnerability scan, SAST
- **8.1.3 OpenShift and Lifecycle Operator Compatibility** — restricted SCC compliance, lifecycle operator CRD fields, mapping validation to catalog metadata

#### 8.2 Preparing Catalog Metadata
- **8.2.1 Required and Recommended Fields** — name, provider, description, version, transports, artifacts, deploymentMode
- **8.2.2 Tool Definitions** — tool name, description, accessType, parameters
- **8.2.3 Runtime Metadata** — default port, MCP path, args, prerequisites (serviceAccount, secrets, configMaps), resources, health endpoints, capabilities
- **8.2.4 Security Indicators** — verifiedSource, secureEndpoint, sast, readOnlyTools
- **8.2.5 Optional Enrichment** — readme, logo, tags, documentation URL, repository URL, license

#### 8.3 Publishing to the MCP Catalog
Create `mcp-catalog-sources` ConfigMap with server metadata.

#### 8.4 Deploying, Registering, and Making Available
Deploy via MCPServer CR, register with gateway via HTTPRoute and MCPServerRegistration, configure AuthPolicies.

### 09-best-practices.md
Security considerations and topology guidance.

#### 9.1 Security Considerations and Recommended Patterns
- **9.1.1 Design tool subsets for different audiences** — admin, specialized, basic tool sets via VirtualMCPServers
- **9.1.2 VirtualMCPServers are not an access control mechanism** — they filter visibility, not authorization. Four-layer authorization stack explained.

#### 9.2 Gateway Topology: When to Use Gateway-per-Team
Start with shared gateway for simplicity. Graduate to gateway-per-team for:
- Multi-team compliance environments
- Sensitive or untrusted workloads
- Blast radius containment
- Independent lifecycle management

### 10-sources-references.md
Links to repositories, demos, documentation, and specifications.

- Experimentation repository
- Setup and usage demos
- MCP Gateway (Kuadrant), MCP Servers, MCP Lifecycle Operator
- AI Stack (OGX, vLLM, RHOAI)
- Identity & Secrets (Keycloak, Vault)
- Platform Infrastructure (Istio, cert-manager, Gateway API)
- Specifications (MCP, Gateway API)
- Documentation links for all components

### 11-appendix-component-versions.md
**WARNING: This is a 3MB file of version tables. Only read if you need specific version information.**

Component versions validated during experimentation (ROSA, OpenShift 4.21.6, RHOAI 3.4.0, last validated 2026-04-21). Includes versions for:
- Platform operators (Service Mesh, Connectivity Link, Authorino, Limitador)
- MCP Gateway components
- Identity and secrets (Keycloak, Vault)
- AI stack (OGX, vLLM)
- Additional infrastructure components

## Snippets Directory

### snippets/05-supported-workflows/
Contains 19 code/YAML snippets referenced in section 5 (setup and usage workflows). These include:
- Operator activation manifests
- Keycloak realm imports
- Gateway Helm configurations
- AuthPolicy examples
- Vault setup scripts
- VirtualMCPServer definitions
- MCPServer CR examples

### snippets/08-bring-your-own-mcp-server/
Contains 3 code snippets referenced in section 8 (bring your own MCP server). These include:
- Tool definition examples
- Runtime metadata structures
- Security indicator templates

## How to Use This Guide

**For platform engineers**: Start with sections 1-4 for context, then follow section 5.1 setup workflows in order. Reference section 6 for responsibility patterns and section 7 for topology decisions.

**For AI engineers**: Read sections 1-3 for context, then jump to section 5.2 usage workflows. Section 6 explains your role and responsibilities.

**For custom MCP server developers**: Read sections 1-4 for architecture context, then follow section 8 for packaging, validation, and publishing. Reference section 5.2.6 for catalog integration.

**For LLM agents**: Use this README to identify which file contains the information you need, then read that specific file. Do not try to read all files at once.
