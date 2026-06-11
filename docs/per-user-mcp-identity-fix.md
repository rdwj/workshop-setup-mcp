# Per-User MCP Identity: End-to-End Fix Description

Date: 2026-06-10. Cluster: cluster-n7pd5 (sandbox5167).
Companion docs: [`per-user-mcp-identity.md`](per-user-mcp-identity.md)
(the original investigation) and
the restructured workshop core path (the fix was folded into modules
[`02-mcp-gateway`](../deploy/workshop/02-mcp-gateway/),
[`05-gateway-registration`](../deploy/workshop/05-gateway-registration/),
[`08-authpolicies`](../deploy/workshop/08-authpolicies/), and
[`11-vault`](../deploy/workshop/11-vault/) — see
[`workshop-restructure-plan.md`](workshop-restructure-plan.md)).

## The Goal

Many developers run Claude Code (or another MCP client) against one
central MCP Gateway. The gateway handles auth and token passing. Everything
a developer does through a backend MCP server — K8s API calls, GitHub API
calls — should happen *as that developer*, with per-user authorization and
a per-user audit trail.

## What Was Wrong: The Design

The investigation doc had concluded there was an architecture constraint:
any header manipulation in the AuthPolicy affected both the client path
and the broker's internal hairpin path, so credentials could not be passed
to backends without breaking the broker. Cross-referencing the design
guides in `guides/MCP-Ecosystem/` (sections 5.1.4.1, 5.1.5, 5.1.6.7) and
the upstream Kuadrant `vault-token-exchange` guide showed this constraint
is not architectural. It was created by two deployment decisions:

1. **One atomic Gateway-wide AuthPolicy did everything.** JWT validation,
   wristband signing, VirtualMCPServer routing, and Authorization-header
   stripping all lived in a single policy attached to the backend
   (`mcps`) listener. Stripping is a *per-server* decision — the OpenShift
   MCP server chokes on a Keycloak JWT, while the GitHub MCP server wants
   a Bearer token — but applied gateway-wide it destroyed the user's JWT
   on every leg, for every backend. That JWT is exactly the credential the
   per-server policies need (Vault login, K8s passthrough).

2. **Clients entered through the backend servers' hostnames.** There was
   no client-facing listener or Route. Clients connected to the MCP
   server's own HTTPRoute hostname and relied on ext_proc to divert them
   to the broker. With the client leg and the hairpin leg sharing one
   listener, one hostname, and one route, no policy could treat them
   differently — hence the apparent "constraint."

The intended design is layered:

- **Client plane** — a dedicated `mcp` listener and a single public
  hostname (`mcp-gateway.<domain>`) behind an edge-TLS Route. The
  gateway-level AuthPolicy lives here: JWT validation, wristband,
  VirtualMCPServer routing. The user's JWT passes through untouched.
- **Backend plane** — the `mcps` listener carries only the broker's
  hairpin traffic to backend routes on internal `*.mcp.local` hostnames.
  A fail-closed catch-all policy (JWT required + tool enforcement + strip
  Authorization) covers any route without its own policy; per-route
  AuthPolicies atomically override it per server: strip for the OpenShift
  server (shared SA) or pass through (per-user K8s identity via External
  OIDC), inject a per-user Vault PAT for GitHub.

The broker forwards the user's Authorization header on the hairpin leg —
the upstream Vault pattern depends on reading it there — so the layered
split is what makes per-user backend credentials possible at all.

## What Was Also Wrong: The Cluster

The dev cluster's gateway dataplane was broken before any policy work —
every `/mcp` request returned 504. Four independent faults, found and
fixed in order:

1. **Stale kagenti v0.5 stack fighting the v0.6 operator.** A leftover
   helm-era ext_proc EnvoyFilter (`istio-system/mcp-gateway`) pointed at
   the dead v0.5 broker service, stacked on top of the operator's correct
   EnvoyFilter on the same listener port. Every request had to clear both
   external processors; one was unreachable; `failure_mode_allow: false`
   turned that into a 504 at exactly the 10s `message_timeout`. Removed
   the old EnvoyFilter, the v0.5 broker-router and controller deployments,
   their services, and the kagenti `mcp-route`.

2. **Istio ambient JWT policies acting as a blanket deny.** Earlier
   experiments left `RequestAuthentication` + DENY `AuthorizationPolicy`
   pairs (`*-require-jwt`) using HTTP attributes (`notRequestPrincipals`,
   `paths`). ztunnel cannot enforce HTTP attributes; its own status
   condition said the policy was "enforced without the HTTP rules… more
   restrictive than requested" — i.e., deny (nearly) everything in
   `mcp-system`. These duplicated what the Kuadrant AuthPolicy does
   correctly at the gateway. Deleted all four, and set
   `istio.io/use-waypoint: none` on `mcp-system` since nothing needs the
   waypoint anymore.

3. **Stale ambient inpod state.** ztunnel inbound delivery to the broker
   and OpenShift MCP server pods timed out (`connection failed: deadline
   has elapsed`) — both pods had RESTARTS=1. Recreating the pods restored
   delivery. (Diagnostic signature: 504 at exactly the ext_proc timeout,
   ztunnel access logs on the destination node show the inbound error.)

4. **Kuadrant operator CrashLoopBackOff.** OOMKilled at a 300Mi limit,
   2,500 restarts over 21 days. With it down, AuthPolicies were *accepted*
   but never *reconciled* — policy edits silently did nothing. Raised
   limits to 1Gi/1CPU; pod has been stable since. (If OLM reverts the
   deployment on CSV resync, patch the `rhcl-operator.v1.3.3` CSV instead.)

This cluster state also explains the original mystery 4xx: the broker's
"server returned 4xx for initialize" wrapped lower-layer ztunnel/ext_proc
failures — the outer status code was not the real error.

## What Was Built

**Keycloak (realm `mcp`)** — the realm was nearly empty (no users, no
groups, no tool roles), so per-user identity had nothing to attach to.
Created: groups `mcp-admins`/`mcp-users`; users `developer-a` and
`developer-b`; a bearer-only client named `openshift-mcp/openshift-mcp-server`
(exactly matching the MCPServerRegistration's namespaced name) with one
client role per discovered tool (14 total); role assignments —
developer-a gets all 14, developer-b a read-only subset of 6
(namespaces_list, projects_list, pods_list, pods_list_in_namespace,
pods_get, pods_log); an optional `groups` client scope on the `mcp-agent`
login client. Tool permissions are now Keycloak admin operations — no
policy edits needed to grant or revoke a tool.

**Gateway plane** — re-hosted the `mcp` listener from the kagenti dev
hostname to `mcp-gateway.apps.cluster-n7pd5...`; created the edge-TLS
OpenShift Route (to the Istio gateway service, never the broker directly)
and the client HTTPRoute (`/mcp` + `/.well-known` → broker, with CORS
headers). The `mcps` listener keeps `*.mcp.local` for backend routes.

**Policies** — deleted the old `mcps`-targeted gateway policy and the
JWT-only per-route policy; applied three:

| Policy | Attaches to | Does |
|---|---|---|
| `mcp-gateway-client-auth` | Gateway listener `mcp` | JWT auth, tool-roles Rego, `x-auth-username`, `/.well-known` exemption, 401/403 bodies |
| `mcp-gateway-backend-auth` | Gateway listener `mcps` (atomic defaults) | JWT auth, tool-roles Rego, **strips Authorization** — fail-closed default for any backend route |
| `openshift-mcp-route-auth` | HTTPRoute `openshift-mcp-server` | JWT auth, tool-roles Rego, strips Authorization (SA mode — see follow-ups) |

One CRD gotcha surfaced: `spec.when` and `spec.defaults` are mutually
exclusive on AuthPolicy, so the client policy uses the implicit
`spec.rules` form (it needs `when` for the OAuth discovery exemption).

## Amendment (2026-06-11, end-to-end run on cluster-mb5pm)

The full-workshop fidelity run surfaced one correction to the policy
attachment model: the router (ext_proc) runs before auth and rewrites the
Host header to `mcp.mcp.local`, so **the client-plane policy must attach to
the broker's HTTPRoute (`mcp-gateway-route`), not the `mcp` listener** — a
listener-attached policy never executes for `/mcp` traffic (symptom: 401s
work but no wristband is issued and tool filtering silently does nothing).
With the policy on the broker route, per-user filtering was verified live:
developer-a and developer-b see different tool sets, and a denied
`tools/call` is rejected by the per-route Rego on the hairpin initialize
(surfaced by the broker as a 500 wrapping the 403). The run also proved the
complete per-user chain on both backends: K8s audit log attribution
(`user=developer-a`, via `pods_run`) and per-user GitHub write scoping via
Vault-injected PATs.

## Verification

All through the public client URL
`https://mcp-gateway.apps.cluster-n7pd5.../mcp`:

| Test | Result |
|---|---|
| No token, `tools/list` | 401, "Authentication required" |
| developer-a: initialize + tools/list | 200, session established, 14 tools |
| developer-a: `tools/call namespaces_list` | Real namespace listing returned through the full hairpin |
| developer-b: `tools/call projects_list` (in role set) | Real project listing |
| developer-b: `tools/call resources_list` (not in role set) | **403 "Insufficient tool permissions"** |

The developer-b 403 carries the *per-route* policy's response body — which
proves ext_proc injects `x-mcp-toolname`/`x-mcp-servername` on the hairpin
leg and the per-route policy is the operative enforcement point. The
client-plane copy of the Rego is defense in depth.

## Follow-Ups

1. **Per-user K8s identity** (the headline goal for the OpenShift server):
   configure External OIDC (now workshop Module 7) so Keycloak JWTs are valid
   K8s tokens, add the group→ClusterRole bindings, then swap
   `openshift-mcp-route-auth` for the passthrough variant
   (`authpolicy-openshift-route-passthrough.yaml`). Mechanically this is
   now a one-file change — the hairpin already carries the user's JWT.
2. **Per-user GitHub identity**: deploy the GitHub MCP server + Vault
   (now workshop Modules 10/11) and apply `authpolicy-github-route-vault.yaml` —
   per-user PATs keyed by `preferred_username`, injected at request time.
3. **Discovery filtering**: generate wristband keys, patch the
   MCPGatewayExtension `trustedHeadersKey`, add the wristband block to the
   client policy, and create MCPVirtualServers — cosmetic tool-list
   filtering on top of the already-enforced call-time control.
4. **Housekeeping**: the kagenti helm release still nominally owns the
   Gateway and Route (edited in place); a future `helm upgrade kagenti`
   could revert them.
