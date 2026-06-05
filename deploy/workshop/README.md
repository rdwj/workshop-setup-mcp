# MCP Ecosystem on Red Hat OpenShift AI

Hands-on workshop for deploying and operating the Model Context Protocol
(MCP) ecosystem on OpenShift, including gateway-based tool federation,
identity-driven access control, and agent integration.

## What You Will Learn

1. Deploy and configure the Kuadrant MCP Gateway for tool federation
2. Deploy MCP servers from the catalog and register them with the gateway
3. Set up identity-based tool access control using Keycloak, OPA Rego (a declarative policy language for access control), and wristband signing
4. Connect a live agent through the authenticated gateway and observe tool filtering

## Prerequisites

- OpenShift 4.16+ cluster with RHOAI 3.4 installed (use `deploy/base`
  kustomize overlay if starting from scratch)
- `oc` CLI authenticated as cluster-admin
- `openssl` available on your workstation (for key generation in Module 6)
- `python3` available on your workstation (for JSON parsing in test commands; `jq` works as an alternative)
- `helm` available on your workstation (for optional Module 8)
- A `MODEL_ENDPOINT` URL for an OpenAI-compatible model that supports tool
  calling (see Module 0)

## Architecture

```
                                                 MCP Server
                                                 (OpenShift K8s tools)
                                                      ^
                                                      |
Browser --> Chat UI --> Gateway Proxy --> Agent --> MCP Gateway --> MCP Server
                                                      ^
                                                      |
                                              Keycloak JWT validation
                                              OPA Rego tool filtering
                                              Wristband token signing
```

The MCP Gateway sits between agents and MCP servers. It provides:
- **Federation** -- A single `/mcp` endpoint for multiple backend MCP servers
- **Authentication** -- JWT validation via Kuadrant's Authorino integration
- **Authorization** -- Per-group tool filtering using OPA Rego policies
  encoded in wristband tokens

Agents never connect to MCP servers directly in production. The gateway
controls which tools each identity can see and invoke.

## Duration

Core modules (1--7): 2--3 hours
Optional modules (8--9): 1 hour each

## Modules

| Module | Directory | Description | Time |
|--------|-----------|-------------|------|
| 0 | `00-model-endpoint/` | Set up a model endpoint for agent testing | 10 min |
| 1 | `01-gateway-infrastructure/` | Deploy Istio, Kuadrant, and the Gateway API infrastructure | 30 min |
| 2 | `02-mcp-gateway/` | Install the MCP Gateway Operator and create the gateway with MCPGatewayExtension | 20 min |
| 3 | `03-mcp-server-prerequisites/` | Create the ServiceAccount, ClusterRoleBinding, and ConfigMap that the MCP server requires | 10 min |
| 4 | `04-mcp-server/` | Deploy the OpenShift MCP server from the catalog | 10 min |
| 5 | `05-gateway-registration/` | Register the MCP server with the gateway via MCPServerRegistration and HTTPRoute | 15 min |
| 6 | `06-identity-auth/` | Install Keycloak, configure realm/groups, generate wristband keys, apply AuthPolicy | 30--45 min |
| 7 | `07-agent-test/` | Reconfigure the pre-deployed agent to use the gateway, test admin vs user tool access | 15--20 min |
| 8 (optional) | `08-vault/` | Add HashiCorp Vault for secret injection into MCP tool calls | 45 min |
| 9 (optional) | `09-external-model/` | Connect the Gen AI Studio Playground to a remote vLLM model with MCP tools | 45 min |

## Real-World Deployment Patterns

This workshop uses production RHOAI 3.4 and MCP Gateway 0.6.0 components.
You'll encounter several real-world deployment patterns that require manual
intervention -- these are documented inline with step-by-step workarounds.
These scenarios reflect actual production deployments and build operational
knowledge.

**Service Endpoint Selection:**
- The MCP Gateway creates two services in `mcp-system`. Always give clients
  the Istio gateway service (`mcp-gateway-<gatewayclass-name>`) — not the
  broker service (`mcp-gateway`). The broker responds to `tools/list` from
  its cache, but `tools/call` only works through the Istio gateway's
  ext_proc routing. This applies to agents, the Playground ConfigMap, and
  any MCP client connecting to the gateway.

**Module 3 -- MCP Server Prerequisites:**
- The lifecycle operator intentionally does not create security-sensitive
  resources (ServiceAccount, ClusterRoleBinding, ConfigMap) to prevent
  privilege escalation. The platform engineer provisions these before
  deployment. See [Layered Authorization Model](mcp-layered-authorization.md).

**Module 4 -- MCP Server Deployment:**
- The built-in catalog image (`registry.redhat.io/.../openshift-mcp-server-rhel9:0.2`)
  does not exist. You must patch to the working image from Quay.
- The lifecycle operator OOMKills at the default 128Mi limit. Patch to 512Mi.

**Module 5 -- Gateway Registration:**
- The broker hardcodes the Istio service name as `<gateway>-istio`. If your
  GatewayClass has a different name, set `privateHost` on the
  MCPGatewayExtension.
- The broker does not auto-reload when the config Secret changes. Restart
  the broker pod after registering servers.
- `prefix` on MCPServerRegistration is immutable once set. Delete and
  recreate if you need to change it.

**Module 6 -- Identity/Auth:**
- RHBK operator only supports OwnNamespace install mode.
- Tokens must include `scope=openid groups` or group-based routing fails
  silently.
- The gateway forwards the `Authorization` header to backends, breaking
  ServiceAccount auth. The AuthPolicy must strip it.
- Wristband `allowed-capabilities` must use unprefixed tool names keyed by
  MCPServerRegistration name. Including the prefix causes double-prefixing.

**Module 9 -- External Model (if attempted):**
- The `gen-ai-aa-mcp-servers` ConfigMap must use the Istio gateway service
  URL, not the broker service URL. The Playground forwards auth tokens
  correctly — earlier failures were caused by using the wrong service
  endpoint (see "Service Endpoint Selection" above).

Full details: [Deployment Findings](https://github.com/rdwj/workshop-setup/blob/main/docs/mcp-gateway-lessons-learned.md)

## Cluster Automation

The `deploy/base/` directory contains a Kustomize overlay that installs
platform prerequisites (RHOAI, NFD, GPU operator, Authorino, Web Terminal).
For instructor-led workshops, apply this before the session:

```bash
# First pass: creates namespaces and operator subscriptions
oc apply -k deploy/base --context="$CTX"

# Wait for operator CRDs to become available
# (RHOAI, NFD, and Authorino operators must finish installing)

# Second pass: creates operand CRs (DataScienceCluster, etc.)
oc apply -k deploy/base --context="$CTX"
```

For repeatable multi-cluster provisioning, point an ArgoCD Application at
`deploy/base/` (or a site-specific overlay under `deploy/overlays/`).

The workshop modules are designed to be followed manually so students
understand each component. The Kustomize base and ArgoCD are for repeatable
provisioning, not for the workshop itself.
