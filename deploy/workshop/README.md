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
- `openssl` available on your workstation (for key generation in Module 5)
- `python3` available on your workstation (for JSON parsing in test commands; `jq` works as an alternative)
- `helm` available on your workstation (for optional Module 7)
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

Core modules (1--6): 2--3 hours
Optional modules (7--8): 1 hour each

## Modules

| Module | Directory | Description | Time |
|--------|-----------|-------------|------|
| 0 | `00-model-endpoint/` | Set up a model endpoint for agent testing | 10 min |
| 1 | `01-gateway-infrastructure/` | Deploy Istio, Kuadrant, and the Gateway API infrastructure | 30 min |
| 2 | `02-mcp-gateway/` | Install the MCP Gateway Operator and create the gateway with MCPGatewayExtension | 20 min |
| 3 | `03-mcp-server/` | Deploy the OpenShift MCP server from the catalog (with manual prerequisites) | 20 min |
| 4 | `04-gateway-registration/` | Register the MCP server with the gateway via MCPServerRegistration and HTTPRoute | 15 min |
| 5 | `05-identity-auth/` | Install Keycloak, configure realm/groups, generate wristband keys, apply AuthPolicy | 30--45 min |
| 6 | `06-agent-test/` | Reconfigure the pre-deployed agent to use the gateway, test admin vs user tool access | 15--20 min |
| 7 (optional) | `07-vault/` | Add HashiCorp Vault for secret injection into MCP tool calls | 45 min |
| 8 (optional) | `08-external-model/` | Connect the Gen AI Studio Playground to a remote vLLM model with MCP tools | 45 min |

## Real-World Deployment Patterns

This workshop uses production RHOAI 3.4 and MCP Gateway 0.6.0 components.
You'll encounter several real-world deployment patterns that require manual
intervention -- these are documented inline with step-by-step workarounds.
These scenarios reflect actual production deployments and build operational
knowledge.

**Module 3 -- MCP Server Deployment:**
- The built-in catalog image (`registry.redhat.io/.../openshift-mcp-server-rhel9:0.2`)
  does not exist. You must patch to the working image from Quay.
- The RHOAI dashboard does not auto-create prerequisites (ServiceAccount,
  ConfigMap) that the lifecycle operator requires. Create them manually.
- The lifecycle operator OOMKills at the default 128Mi limit. Patch to 512Mi.

**Module 4 -- Gateway Registration:**
- The broker hardcodes the Istio service name as `<gateway>-istio`. If your
  GatewayClass has a different name, set `privateHost` on the
  MCPGatewayExtension.
- The broker does not auto-reload when the config Secret changes. Restart
  the broker pod after registering servers.
- `toolPrefix` on MCPServerRegistration is immutable once set. Delete and
  recreate if you need to change it.

**Module 5 -- Identity/Auth:**
- RHBK operator only supports OwnNamespace install mode.
- Tokens must include `scope=openid groups` or group-based routing fails
  silently.
- The gateway forwards the `Authorization` header to backends, breaking
  ServiceAccount auth. The AuthPolicy must strip it.
- Wristband `allowed-tools` must use unprefixed tool names keyed by
  MCPServerRegistration name. Including the prefix causes double-prefixing.

**Module 8 -- External Model (if attempted):**
- The Gen AI Playground does not forward MCP auth tokens to the gateway.
  Register MCP servers with their direct ClusterIP URL instead.

Full details: [Deployment Findings](https://github.com/rdwj/workshop-setup/blob/main/docs/mcp-gateway-lessons-learned.md)

## Cluster Automation

The `ansible/` directory contains playbooks that automate the full stack
deployment. For instructor-led workshops, run these before the session:

```bash
cd ansible
ansible-playbook gateway-infrastructure.yml -i inventory/<your-inventory>.yml
ansible-playbook mcp-setup.yml -i inventory/<your-inventory>.yml
ansible-playbook ecosystem-setup.yml -i inventory/<your-inventory>.yml
```

The workshop modules are designed to be followed manually so students
understand each component. The ansible playbooks are for repeatable
provisioning, not for the workshop itself.
