## **Sources & References**

### **Experimentation Repository**

* [jaideepr97/mcp-ecosystem-experimentation](https://github.com/jaideepr97/mcp-ecosystem-experimentation/tree/openshift) (branch: `openshift`) — infrastructure manifests, scripts, and configuration used to validate the patterns documented here.

### **Demos**

* [MCP Ecosystem Setup Demo](https://youtu.be/ncwSVtc_6Qo) — walkthrough of platform engineer setup: operators, gateway, Keycloak, Vault, AuthPolicies, and VirtualMCPServers.  
* [MCP Ecosystem Usage Demo](https://youtu.be/Fb3GXLJxfyQ) — end-to-end usage: catalog browsing, server deployment, Playground interaction, and tool invocation.

### **MCP Gateway (Kuadrant)**

| Project | Repository |
| :---- | :---- |
| MCP Gateway Controller | [kuadrant/mcp-controller](https://github.com/kuadrant/mcp-controller) |
| MCP Gateway Helm Charts | [kuadrant/mcp-controller (charts/)](https://github.com/kuadrant/mcp-controller/tree/main/charts) |
| Kuadrant (Connectivity Link) | [Kuadrant/kuadrant-operator](https://github.com/Kuadrant/kuadrant-operator) |
| Authorino | [Kuadrant/authorino](https://github.com/Kuadrant/authorino) |
| Limitador | [Kuadrant/limitador](https://github.com/Kuadrant/limitador) |

### **MCP Servers**

| Project | Repository |
| :---- | :---- |
| OpenShift MCP Server | [openshift/openshift-mcp-server](https://github.com/openshift/openshift-mcp-server) |
| GitHub MCP Server | [github/github-mcp-server](https://github.com/github/github-mcp-server) |

### **MCP Lifecycle Operator**

| Project | Repository |
| :---- | :---- |
| MCP Lifecycle Operator | [openshift/mcp-lifecycle-operator](https://github.com/openshift/mcp-lifecycle-operator) (branch: `release-0.1`) |

### **AI Stack**

| Project | Repository |
| :---- | :---- |
| OGX (formerly Llama Stack) | [ogx-ai/ogx](https://github.com/ogx-ai/ogx) |
| vLLM | [vllm-project/vllm](https://github.com/vllm-project/vllm) |
| Red Hat OpenShift AI | [RHOAI product page](https://www.redhat.com/en/technologies/cloud-computing/openshift/openshift-ai) |

### **Identity & Secrets**

| Project | Repository |
| :---- | :---- |
| Keycloak | [keycloak/keycloak](https://github.com/keycloak/keycloak) |
| HashiCorp Vault | [hashicorp/vault](https://github.com/hashicorp/vault) |

### **Platform Infrastructure**

| Project | Repository |
| :---- | :---- |
| Istio (Service Mesh) | [istio/istio](https://github.com/istio/istio) |
| cert-manager | [cert-manager/cert-manager](https://github.com/cert-manager/cert-manager) |
| Kubernetes Gateway API | [kubernetes-sigs/gateway-api](https://github.com/kubernetes-sigs/gateway-api) |

### **Specifications**

* [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) — the open standard for AI model tool integration.  
* [Kubernetes Gateway API](https://gateway-api.sigs.k8s.io/) — the Kubernetes-native API for managing network gateways and routing.

### **Documentation**

**MCP Gateway & Kuadrant**

* [MCP Gateway README](https://github.com/kuadrant/mcp-controller/blob/main/README.md) — installation, Helm chart usage, and CRD reference.  
* [Kuadrant AuthPolicy docs](https://docs.kuadrant.io/latest/kuadrant-operator/doc/reference/authpolicy/) — AuthPolicy CRD reference for authentication and authorization.  
* [Authorino architecture](https://docs.kuadrant.io/latest/authorino/docs/architecture/) — how Authorino evaluates auth pipelines (identity, metadata, authorization, response).

**OpenShift MCP Server**

* [Getting Started on Kubernetes](https://github.com/openshift/openshift-mcp-server/blob/main/docs/getting-started-kubernetes.md) — ServiceAccount-based deployment guide.  
* [Configuration reference](https://github.com/openshift/openshift-mcp-server/blob/main/docs/configuration.md) — toolsets, denied resources, read-only mode.

**Identity & Secrets**

* [Vault KV Secrets Engine v2](https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2) — the secrets engine used for per-user credential storage.  
* [Vault JWT/OIDC Auth Method](https://developer.hashicorp.com/vault/docs/auth/jwt) — JWT authentication used by the AuthPolicy metadata evaluators.  
* [Keycloak Server Administration Guide](https://www.keycloak.org/docs/latest/server_admin/) — realm, client, group, and protocol mapper configuration.

**Platform**

* [OpenShift AI Documentation](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/) — RHOAI installation, DataScienceCluster, and Gen AI Studio.  
* [Kubernetes Gateway API Reference](https://gateway-api.sigs.k8s.io/reference/spec/) — HTTPRoute, Gateway, and GatewayClass specs.  
* [OpenShift Service Mesh Documentation](https://docs.redhat.com/en/documentation/openshift_service_mesh/) — Istio integration, EnvoyFilter, and ext\_proc configuration.

