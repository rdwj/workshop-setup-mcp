# MCP Ecosystem on Red Hat OpenShift AI

Hands-on workshop for deploying and operating the Model Context Protocol
(MCP) ecosystem on OpenShift, including gateway-based tool federation,
identity-driven access control, and agent integration.

## What You Will Learn

1. Deploy and configure the Kuadrant MCP Gateway for tool federation
2. Deploy MCP servers from the catalog and register them with the gateway
3. Set up identity-based tool access control using Keycloak, OPA Rego (a declarative policy language for access control), and wristband signing
4. Make per-user identity flow end to end — developers on Claude Code act as themselves at the gateway, the K8s API (External OIDC + RBAC + audit), and GitHub (Vault-injected per-user PATs)
5. Connect a live agent through the authenticated gateway and observe tool filtering

## Prerequisites

Before starting, verify your workstation tools (`oc`, `helm`, `openssl`,
`python3`) and cluster access. See the
[Prerequisites checklist](prerequisites/README.md) for verification
commands and download links.

## Architecture

Visual versions — token flow and end-state system:
[architecture-diagrams.html](architecture-diagrams.html)
(served on the published site at `/architecture-diagrams.html`).

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

Core path (Modules 0--9): 3.5--4.5 hours — ends at the per-user-identity
milestone (Claude Code against the gateway). No GPU or model required.
Track B (Modules 10--11): ~1.5 hours — per-user GitHub credentials.
Track C (Modules 12--18): model + agent + Playground (optional).
Track D (Modules 19--20): observability + extension exercise (optional).

## Modules

### Core path (required)

| Module | Directory | Description | Time |
|--------|-----------|-------------|------|
| 0 | `00-cluster-prerequisites/` | Install RHOAI, Service Mesh, and platform operators | 15--20 min |
| 1 | `01-gateway-infrastructure/` | Deploy Istio, Kuadrant, and the Gateway API infrastructure | 30 min |
| 2 | `02-mcp-gateway/` | MCP Gateway operator + two-plane Gateway (client/backend listeners) + the single client URL | 25 min |
| 3 | `03-mcp-server-prerequisites/` | ServiceAccount, ClusterRoleBinding, write-enabled server config | 10 min |
| 4 | `04-mcp-server/` | Deploy the OpenShift MCP server from the catalog | 10 min |
| 5 | `05-gateway-registration/` | Register the server (internal `.mcp.local` hostname), VirtualMCPServers, rate limits | 15 min |
| 6 | `06-identity-keycloak/` | Keycloak, realm, groups, users, per-tool client roles, wristband keys | 30--45 min |
| 7 | `07-external-oidc/` | Keycloak JWTs become K8s API tokens; group→RBAC mapping (break-glass first!) | 20--30 min |
| 8 | `08-authpolicies/` | Layered enforcement: client-plane policy, backend default, per-route passthrough | 20--30 min |
| 9 | `09-developer-onboarding/` | **The milestone**: Claude Code against the gateway — per-user tools, RBAC, audit | 20--30 min |

### Track B — second backend, per-user credentials

| Module | Directory | Description | Time |
|--------|-----------|-------------|------|
| 10 | `10-github-mcp-server/` | GitHub MCP server (shared discovery PAT, read-only) | 20 min |
| 11 | `11-vault/` | Vault + per-user GitHub PAT injection at request time | 45--60 min |

### Track C — model + agent (optional, needs GPU or external model)

| Module | Directory | Description | Time |
|--------|-----------|-------------|------|
| 12 | `12-gpu-node/` | Add a GPU compute node and HardwareProfile | 15--20 min |
| 13 | `13-models-as-a-service/` | Deploy MaaS infrastructure (PostgreSQL, Gateway, TLS, DSC patch) | 30--45 min |
| 14 | `14-deploy-model/` | Deploy gpt-oss-20b from the Red Hat Model Catalog via KServe | 15--25 min |
| 15 | `15-model-endpoint/` | Set up a model endpoint for agent testing | 10 min |
| 16 | `16-deploy-agent/` | Build and deploy the agent, gateway proxy, and chat UI | 15--20 min |
| 17 | `17-agent-test/` | Test admin vs user tool access through the gateway | 15--20 min |
| 18 | `18-playground/` | Gen AI Studio Playground with an external model and MCP tools | 45 min |

### Track D — ops + extension (optional)

| Module | Directory | Description | Time |
|--------|-----------|-------------|------|
| 19 | `19-observability/` | Cluster Observability Operator, Perses dashboards, Loki logging | 45 min |
| 20 | `20-add-mcp-server/` | Add a third-party MCP server with per-tool access control | 30 min |

## Real-World Deployment Patterns

This workshop uses production RHOAI 3.4 and MCP Gateway 0.6.0 components.
You'll encounter several real-world deployment patterns that require manual
intervention -- these are documented inline with step-by-step workarounds.
These scenarios reflect actual production deployments and build operational
knowledge.

**One Client URL:**
- Every consumer — Claude Code, the agent, the Playground ConfigMap —
  connects to `https://mcp-gateway.<CLUSTER_DOMAIN>/mcp` (created in
  Module 2). This edge-TLS Route fronts the Istio gateway service (Envoy),
  never the broker directly: the broker responds to `tools/list` from its
  cache, but `tools/call` only works through ext_proc routing, and
  traffic that bypasses Envoy bypasses every AuthPolicy.

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
- `toolPrefix` on MCPServerRegistration is immutable once set. Delete and
  recreate if you need to change it.

**Modules 6--8 -- Identity and AuthPolicies:**
- RHBK operator only supports OwnNamespace install mode.
- Tokens must include `scope=openid groups` or group-based routing fails
  silently.
- Authorization-header handling is **per backend route**, not gateway-wide:
  the backend-plane default strips it; the OpenShift route passes it
  through (per-user K8s identity); the GitHub route (Module 11) replaces it
  with a per-user PAT from Vault. Stripping it gateway-wide breaks
  everything per-user — see `docs/per-user-mcp-identity-fix.md`.
- Wristband `allowed-tools` must use unprefixed tool names keyed by
  MCPServerRegistration name. Including the prefix causes double-prefixing.
- External OIDC (Module 7) invalidates all cached `oc` tokens — prepare
  break-glass access first.

**Module 18 -- Gen AI Playground (if attempted):**
- The `gen-ai-aa-mcp-servers` ConfigMap uses the same client URL as
  everything else (see "One Client URL" above).

Full details: [Deployment Findings](https://github.com/rdwj/workshop-setup/blob/main/docs/mcp-gateway-lessons-learned.md)

## Running the Workshop with an Agent

For agent-driven end-to-end runs, copy `workshop-answers.example.yaml`
(repo root) to `workshop-answers.yaml` (gitignored) and fill in the
credentials and run decisions. It contains everything the agent needs that
isn't derivable on-cluster: track selection, the External OIDC break-glass
procedure, GitHub PATs, model endpoint choices, and the per-milestone
verification checks.

## Cluster Automation

Module 0 walks students through installing platform prerequisites using
`deploy/base/`. For instructor-led workshops, the instructor can apply
this before the session so students can skip Module 0.

For repeatable multi-cluster provisioning, point an ArgoCD Application at
`deploy/base/` (or a site-specific overlay under `deploy/overlays/`).

The workshop modules are designed to be followed manually so students
understand each component. The Kustomize base and ArgoCD are for repeatable
provisioning, not for the workshop itself.
