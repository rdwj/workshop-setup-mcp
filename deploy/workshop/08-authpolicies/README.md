# Module 8: AuthPolicies — Layered Enforcement

Turn the identities from Modules 6--7 into enforcement. Three Kuadrant
AuthPolicies, one per concern:

| Policy | Attaches to | Responsibility |
|---|---|---|
| `mcp-gateway-client-auth` | **HTTPRoute `mcp-gateway-route`** (the broker's route — see below) | JWT validation, wristband signing, VirtualMCPServer routing, tool-call Rego, `x-auth-username` |
| `mcp-gateway-backend-auth` | Gateway listener `mcps` (backend plane) | Fail-closed default: JWT required, tool-call Rego, **Authorization stripped** for any route without its own policy |
| `openshift-mcp-route-auth` | HTTPRoute `openshift-mcp-server` | JWT + tool-call Rego + **user JWT passes through** to the server → per-user K8s identity |

**Why the client policy attaches to the broker's route, not the `mcp`
listener:** the router (ext_proc) runs first in the Envoy filter chain and
rewrites the Host header from the public hostname to its derived public
host (`mcp.mcp.local`) *before* auth evaluates. Client traffic therefore
matches the operator-managed broker route (`mcp-gateway-route`), and a
policy attached to the `mcp` listener never executes for `/mcp` traffic.
The symptom of getting this wrong is silent: 401s still work (the backend
default fires), but no wristband is issued and every user sees every tool.

This layering is the heart of per-user identity. The client-plane policy
validates the developer's JWT and leaves it intact; the broker forwards it
on the hairpin leg; the per-route policy decides per backend what happens
to it — pass it through (OpenShift, with External OIDC), replace it with a
per-user credential (GitHub via Vault, Module 11), or strip it (the safe
default for everything else). A single gateway-wide policy cannot express
this — see `docs/per-user-mcp-identity-fix.md` for why.

**Time:** 20--30 minutes

**Prerequisites:**
- Modules 2, 5, 6 complete (gateway with two listeners, registration +
  VirtualMCPServers, Keycloak realm + wristband keys)
- Module 7 complete (External OIDC) for the passthrough variant — otherwise
  use the SA variant (see Step 3)

> **Working directory:**
>
> ```bash
> cd deploy/workshop/08-authpolicies
> ```

## Variables

```bash
CTX="<your-kube-context>"
CLUSTER_DOMAIN=$(oc get ingress.config cluster --context="$CTX" -o jsonpath='{.spec.domain}')
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" -o jsonpath='{.spec.host}')"
KEYCLOAK_ISSUER="${KEYCLOAK_URL}/realms/mcp-gateway"
```

---

## Step 1: Apply the Client-Plane Policy

```bash
sed "s|KEYCLOAK_ISSUER|${KEYCLOAK_ISSUER}|g" authpolicy-gateway-client.yaml \
  | oc apply --context="$CTX" -f -
```

!!! warning "Never apply the placeholder literally"

    Always sed-substitute `KEYCLOAK_ISSUER`. Applying the manifest directly
    overwrites a working issuer URL with the literal string and every
    request starts failing 401.

## Step 2: Apply the Backend-Plane Default

```bash
sed "s|KEYCLOAK_ISSUER|${KEYCLOAK_ISSUER}|g" authpolicy-gateway-backend.yaml \
  | oc apply --context="$CTX" -f -
```

Kuadrant `defaults` semantics make this the fail-closed catch-all: any
backend HTTPRoute *without* its own AuthPolicy gets JWT-required +
tool-enforcement + Authorization stripping. A per-route policy atomically
replaces it for that route only.

## Step 3: Apply the Per-Route Policy for the OpenShift MCP Server

**Passthrough (per-user K8s identity — requires Module 7):**

```bash
sed "s|KEYCLOAK_ISSUER|${KEYCLOAK_ISSUER}|g" authpolicy-openshift-route-passthrough.yaml \
  | oc apply --context="$CTX" -f -
```

**Or the shared-SA fallback (if External OIDC is not configured):**

```bash
sed "s|KEYCLOAK_ISSUER|${KEYCLOAK_ISSUER}|g" authpolicy-openshift-route-sa.yaml \
  | oc apply --context="$CTX" -f -
```

Apply exactly one — both define `openshift-mcp-route-auth`, so switching
modes later is just applying the other file.

## Step 4: Verify Policy Status

```bash
oc get authpolicy -A --context="$CTX"
```

All three should show `ACCEPTED=True`. The client and per-route policies show `ENFORCED=True`; the backend default may show `Enforced=False (Overridden)` — see below.

> **"Accepted but never Enforced"?** Check the Kuadrant operator
> (`kuadrant-operator-controller-manager` in `openshift-operators`) — if it
> is CrashLooping (OOMKilled at its default 300Mi limit), policies are
> accepted by the API server but never reconciled into Authorino config.
> Raise its memory limits and re-check.

> **Backend policy shows `Enforced=False` with reason "Overridden"** (or
> "partially enforced") — expected. Per-route policies (client-auth on the
> broker's route, the OpenShift route policy) atomically override the
> default for their routes; once every route under `mcps` has its own
> policy, the default has nothing left to enforce directly but still
> guards any future route.

## Step 5: Verify Enforcement

Unauthenticated requests are rejected at the client plane:

```bash
curl -sk -o /dev/null -w "HTTP %{http_code}\n" \
  "https://mcp-gateway.${CLUSTER_DOMAIN}/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
# Expected: HTTP 401
```

Authenticated requests succeed (token from Module 6 Step 10):

```bash
curl -sk -o /dev/null -w "HTTP %{http_code}\n" \
  "https://mcp-gateway.${CLUSTER_DOMAIN}/mcp" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}'
# Expected: HTTP 200
```

Verify per-user filtering is live (the definitive check that the client
policy is executing): `tools/list` as developer-a and developer-b must
return **different counts** (15 vs 8 on the core path). Full per-user
behavior, including the write demo and denied-call semantics, is exercised
in Module 9.

---

## How Enforcement Works at Call Time

1. ext_proc parses the JSON-RPC `tools/call` payload and injects
   `x-mcp-toolname` (unprefixed) and `x-mcp-servername` (the
   MCPServerRegistration's namespaced name) as headers. Validated
   empirically: these headers are present on the **hairpin leg**, so the
   hard 403 comes from the per-route policy; the client-plane copy of the
   Rego is defense in depth.
2. The Rego looks up `auth.identity.resource_access[servername].roles` and
   allows the call only if the tool name is among the caller's client
   roles. Tool permissions live entirely in Keycloak (Module 6).
3. The wristband (`x-authorized-tools`) and `x-mcp-virtualserver` headers,
   issued on the client plane, drive `tools/list` *filtering* — what users
   see. The Rego drives *enforcement* — what they can call. The broker
   applies the intersection.

### AuthPolicy CRD gotchas (learned the hard way)

- `spec.when` and `spec.defaults` are **mutually exclusive** ("Implicit and
  explicit defaults are mutually exclusive"). The client policy needs
  `when` (the `/.well-known` exemption for OAuth discovery), so it uses the
  implicit `spec.rules` form. The backend policy uses `defaults: strategy:
  atomic` (no `when`) so per-route policies can replace it.
- Authorino's OPA injects its own `default allow := false` — do not add
  your own, and use `arr[_] == val` instead of the `in` keyword.
- Do not use Istio `RequestAuthentication`/`AuthorizationPolicy` with HTTP
  attributes to "double up" JWT enforcement on ambient-mesh namespaces:
  ztunnel cannot enforce HTTP rules and degrades such DENY policies into a
  blanket deny ("enforced without the HTTP rules... more restrictive than
  requested"), which blackholes the gateway↔broker path. Kuadrant
  AuthPolicy at the gateway is the enforcement mechanism in this design.

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| AuthPolicy mcp-gateway-client-auth | mcp-system | Client-plane auth: JWT, wristband, VMS routing, Rego |
| AuthPolicy mcp-gateway-backend-auth | mcp-system | Backend-plane fail-closed default (strip) |
| AuthPolicy openshift-mcp-route-auth | mcp-ecosystem | Per-route: JWT through → per-user K8s identity |

---

**Next**: [Module 9 -- Developer Onboarding](../09-developer-onboarding/README.md)
