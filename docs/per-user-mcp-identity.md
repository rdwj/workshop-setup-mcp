# Per-User MCP Identity: Investigation

Status: **Resolved (2026-06-10)** -- root cause and fix deployed/verified on cluster-n7pd5. Full end-to-end description: [`per-user-mcp-identity-fix.md`](per-user-mcp-identity-fix.md). The fix was folded into the restructured workshop core path (see [`workshop-restructure-plan.md`](workshop-restructure-plan.md)): its manifests now live in the workshop core path: Gateway/client-route in [`02-mcp-gateway`](../deploy/workshop/02-mcp-gateway/), backend routes in [`05-gateway-registration`](../deploy/workshop/05-gateway-registration/), AuthPolicies in [`08-authpolicies`](../deploy/workshop/08-authpolicies/), Vault injection in [`11-vault`](../deploy/workshop/11-vault/). See also [Resolution](#resolution) below.

---

## Problem Statement

The MCP Gateway's AuthPolicy strips the `Authorization` header before forwarding requests to backend MCP servers. All K8s API calls from the OpenShift MCP server run as the shared `mcp-viewer` ServiceAccount. The GitHub MCP server uses a single shared PAT. There is no per-user audit trail or RBAC enforcement at the backend level.

Tool-level access control (which tools each user can see and call) IS enforced per-user via the wristband + VirtualMCPServer + OPA Rego mechanism. The gap is only at the backend K8s/GitHub API call level.

---

## What Was Tried

### 1. External OIDC (Successful -- but does not solve the gateway problem)

Configured OpenShift 4.20 External OIDC so Keycloak JWTs from the `mcp-gateway` realm are valid K8s API tokens. Verified:

- Keycloak users authenticate to the K8s API directly
- developer-a (mcp-admins group) gets cluster-admin
- developer-b (mcp-users group) gets view-only
- K8s RBAC correctly denies write operations for view-only users
- OpenShift console redirects to Keycloak for login

This is deployed on the workshop cluster (Module 10). It proves the tokens work for K8s auth but does not address passing them through the gateway to backend MCP servers.

### 2. Removing the Authorization Header Stripping (Failed)

Removed the `authorization: plain: value: ""` block from the AuthPolicy so the user's JWT would flow through to the backend. The OpenShift MCP server defaults to `passthrough` mode (`cluster_auth_mode`), meaning it uses any incoming Bearer token for K8s API calls.

**Result:** `tools/call` broke immediately. The failure path:

1. Broker receives the request with the user's JWT
2. Broker forwards `tools/call` to the backend via the Istio gateway's internal service (`privateHost`)
3. Broker's internal `initialize` request to the backend returns 4xx
4. Request never reaches the MCP server pod -- failure is at the Envoy proxy layer

Error from the broker: `"failed to create client: transport error: server returned 4xx for initialize POST, likely a legacy SSE server"`

**Root cause:** The header stripping is load-bearing for the broker's internal routing. The broker's internal requests go back through the Istio gateway, where Authorino and the ext_proc run on both the public and private paths. Headers set in the AuthPolicy `response.success.headers` section affect both paths.

```
Client -> Istio Gateway -> Authorino -> Broker
                                          |
                            Broker -> Istio Gateway (private) -> ext_proc -> Backend MCP Server
```

### 3. GitHub MCP Server Per-Request Auth (Confirmed Possible)

Research confirmed the GitHub MCP server (v1.2.0) in HTTP mode supports per-request authentication. Each incoming request's `Authorization: Bearer <token>` is extracted to create a per-request GitHub API client.

Notes:
- `GITHUB_TRUST_PROXY_HEADERS` is unrelated to auth -- controls `X-Forwarded-Host` and `X-Forwarded-Proto` for OAuth discovery URLs only
- `credentialRef` on MCPServerRegistration is broker-only (tool discovery); never injected into `tools/call` requests

---

## Architecture Constraint

> **Superseded (2026-06-10):** This constraint is an artifact of the
> deployment layout, not of the gateway architecture. See
> [Resolution](#resolution).

The core constraint is the broker's internal routing loop. The broker forwards `tools/call` back through the Istio gateway's internal service. Authorino and ext_proc run on both public and private paths. Any header manipulation in the AuthPolicy response affects both paths, making it impossible to pass credentials to backends without also affecting the broker's internal traffic.

---

## What to Try Next

### 1. Token Exchange Inside the MCP Server (Most Promising)

The upstream kubernetes-mcp-server supports `token_exchange_strategy` in `config.toml`:

| Strategy | Provider |
|----------|----------|
| `keycloak-v1` | Keycloak-specific token exchange |
| `rfc8693` | Generic RFC 8693 token exchange |
| `entra-obo` | Microsoft Entra ID On-Behalf-Of flow |

The server receives the user's identity via a custom header (injected by the AuthPolicy), exchanges it for a K8s-compatible token via the identity provider, and uses that for K8s API calls. This avoids the header-stripping problem because the exchange happens inside the MCP server, not at the gateway.

**Requirements:**
- Pass user identity to the MCP server without using the `Authorization` header (stripped). Options: custom header via AuthPolicy `response.success.headers`, MCP session metadata, or Authorino metadata injection.
- Keycloak 26.2+ for GA standard token exchange support.
- Additional Keycloak clients and token exchange permissions.

### 2. Vault-Based Per-User GitHub PAT Injection

Store each user's GitHub PAT in Vault keyed by username. The AuthPolicy performs a conditional Vault HTTP lookup (based on `x-mcp-servername`) and injects the PAT as a custom header. Module 14 already covers Vault integration.

```yaml
metadata:
  vault:
    http:
      urlExpression: >-
        "http://vault.vault.svc:8200/v1/secret/data/"
        + auth.identity.preferred_username
        + "/github"
```

The GitHub MCP server already supports per-request Bearer tokens in HTTP mode, so injecting the PAT as the Authorization header on the backend request would work -- if the internal routing problem (next item) is solved.

### 3. Investigate the Broker's Internal Routing

The 4xx error on the internal path needs deeper investigation:

- Does the Authorino WASM plugin run on the broker's private-path requests?
- Is the 4xx from Authorino, the ext_proc, or Envoy itself?
- Could a separate AuthPolicy (or AuthPolicy exemption) on the private listener fix this?
- Does the broker strip or preserve the Authorization header when creating backend sessions?

A targeted fix (exempting the private path from Authorino) might make simple header pass-through viable after all.

### 4. Customer Context: Entra ID

The customer uses Entra ID, not Keycloak. Relevant:

- The OpenShift MCP server supports `token_exchange_strategy = "entra-obo"` with certificate-based auth
- The External OIDC Authentication CR supports any OIDC-compliant provider
- Both paths should work with Entra ID without Keycloak-specific dependencies

---

## Resolution

The "architecture constraint" above was self-inflicted by two deployment
decisions that deviated from the intended design (guide sections 5.1.4.1,
5.1.5, 5.1.6.7; upstream Kuadrant `vault-token-exchange` guide):

1. **One atomic Gateway-wide AuthPolicy carried per-server concerns.** The
   Authorization stripping is a per-server decision, but it was applied at
   the Gateway (`sectionName: mcps`), destroying the user JWT on every leg
   for every backend. The designed mechanism is **per-route AuthPolicies**
   on each backend HTTPRoute: the broker forwards the user's JWT on the
   hairpin leg, and the per-route policy consumes it there — stripping it
   for the OpenShift server (SA mode), passing it through (per-user K8s
   identity with External OIDC), or exchanging it at Vault for a per-user
   GitHub PAT. The upstream Vault guide's policy reads
   `request.headers["authorization"]` on the backend route — confirming the
   user JWT is *supposed* to be present on the internal leg.

2. **Clients entered through backend-server hostnames.** With no
   client-facing listener/Route, the client leg and hairpin leg shared one
   listener, hostname, and route, so no policy could distinguish them. The
   fix separates the planes: a client `mcp` listener +
   `mcp-gateway.<domain>` Route carrying the JWT/wristband/VirtualMCPServer
   policy, and the `mcps` listener restricted to internal `*.mcp.local`
   backend routes with per-route policies (plus a fail-closed catch-all
   default that strips credentials for unconfigured routes).

The failed Attempt #2 (removing the stripping) broke precisely because the
single shared path applied the change to both legs at once. Note on the
observed error: the 4xx reported by the broker was likely an inner 5xx/4xx
from a lower layer re-emitted upward, so don't trust the outer status code
when debugging — check Authorino, Envoy, and broker logs separately.

Manifests, redeployment steps, verification, and a dev-cluster name mapping
were originally drafted as a standalone module 17; after the workshop restructure, its manifests now live in the workshop core path: Gateway/client-route in [`02-mcp-gateway`](../deploy/workshop/02-mcp-gateway/), backend routes in [`05-gateway-registration`](../deploy/workshop/05-gateway-registration/), AuthPolicies in [`08-authpolicies`](../deploy/workshop/08-authpolicies/), Vault injection in [`11-vault`](../deploy/workshop/11-vault/). Redeployment guidance lives in those modules' READMEs.

One open risk to validate during rollout: mcp-gateway ≥ v0.6 signs the
router's own hairpin backend-init requests with a short-lived HMAC JWT
rather than the user's Keycloak JWT. If those requests fail the per-route
Keycloak-JWT authentication, the per-route policies need a second
authentication method for router-signed JWTs (see the module README's
troubleshooting section).

## Related Files

| File | Relevance |
|------|-----------|
| `deploy/workshop/06-identity-keycloak/authpolicy.yaml` | Authorization header stripping block |
| `deploy/workshop/06-identity-keycloak/authentication-cr.yaml` | External OIDC configuration |
| `deploy/workshop/03-mcp-server-prerequisites/openshift-mcp-prerequisites.yaml` | MCP server config.toml (no auth settings currently) |
| `deploy/workshop/10-github-mcp-server/github-mcp-server.yaml` | GitHub MCP server deployment |
| `deploy/workshop/11-vault/` | Vault integration (planned) |
| `docs/mcp-layered-authorization.md` | Four-layer authorization model (context for this problem) |
