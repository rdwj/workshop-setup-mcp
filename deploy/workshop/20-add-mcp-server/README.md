# Module 20: Adding a Third-Party MCP Server (Optional)

This module walks through adding a new MCP server to the gateway ecosystem end-to-end: deploying the server, registering it with the gateway, configuring tool-level permissions in Keycloak, and demonstrating per-tool access restriction.

The key takeaway: for a server that needs no backend credentials, **no AuthPolicy changes are needed**. The backend-plane default policy (Module 8) covers its route the moment it attaches — JWT required, tool-call enforcement via the generic Rego, Authorization stripped — and tool permissions are managed entirely in Keycloak and VirtualMCPServer resources. (A server that *does* need backend credentials — like GitHub in Module 11 — additionally gets its own per-route AuthPolicy to inject them; that is an add-on, not a change to existing policies.)

## Prerequisites

- Core path (Modules 0--8) completed (gateway, identity, layered AuthPolicies, Keycloak realm with `resource_access` client roles)
- `mcp-ecosystem` namespace with ReferenceGrant for the MCP gateway

## Variables

Set these for the commands below:

```bash
export CTX="<your-kube-context>"
export KEYCLOAK_URL="https://keycloak-keycloak.<CLUSTER_DOMAIN>"
export CLUSTER_DOMAIN="apps.cluster-xxx.xxx.sandboxNNNN.opentlc.com"
```

## Step 1: Deploy the Calculus-Helper Server

Clone and deploy using the server's built-in deploy script:

```bash
git clone https://github.com/rdwj/calculus-helper.git
cd calculus-helper
./deploy.sh mcp-ecosystem
```

This creates a BuildConfig, builds the container image on-cluster, and deploys a Deployment + Service named `mcp-server` in `mcp-ecosystem`.

Verify the pod is running:

```bash
oc get pods -l app=mcp-server -n mcp-ecosystem --context="$CTX"
```

## Step 2: Create the HTTPRoute

The HTTPRoute tells the gateway proxy how to reach the calculus-helper.
Note the backendRef points at the Service that `deploy.sh` actually
creates (`mcp-server`) — a mismatch here leaves the MCPServerRegistration
stuck with `Service ... not found`:

```bash
oc apply -f httproute.yaml --context="$CTX"
```

## Step 3: Register with the Gateway

The MCPServerRegistration tells the broker about the new server. The `toolPrefix` is only applied if names collide with another server's tools (MCP Gateway v0.7.0 behavior) — with no collisions here, tools keep their natural names:

```bash
oc apply -f mcpserverregistration.yaml --context="$CTX"
```

Wait for the registration to show `READY=True`:

!!! warning "Transport compatibility"

    The MCP Gateway broker speaks **streamable-http** and sends a POST for
    `initialize`. A server running the legacy **SSE** transport fails
    registration with `server returned 4xx for initialize POST, likely a
    legacy SSE server`. Make sure the calculus-helper is started with the
    streamable-http transport (fastmcp v3: `transport="streamable-http"`,
    or the equivalent CLI flag) — not `sse`.

!!! warning "Registration can be intermittent with this server"

    Known issue: the calculus-helper registration may flap — READY=True
    with tools discovered, then READY=False after a broker restart, with
    broker logs showing `server does not support listening` or ping
    failures. The broker's keepalive expects the server to hold its
    streamable-http session; this third-party server's session handling is
    not fully stable with broker v0.6. If it flaps: restart the
    calculus-helper pod first, then the broker, and re-check
    `oc get mcpserverregistrations -n mcp-ecosystem`. Tool visibility
    through the gateway follows the registration state — calculus tools
    appear and disappear with it. Treat persistent flapping as a
    server-side issue, not a gateway misconfiguration: the openshift and
    github registrations on the same broker remain READY throughout.

    Also verify the **discovered tool count matches the Keycloak roles**
    you create in Step 4: `tools/list` may show 7 tools while the script
    creates 8 roles (`multivariable_calc` is not exposed by all builds of
    the server). Extra roles are harmless; tools without roles are
    uncallable.

```bash
oc get mcpserverregistrations -n mcp-ecosystem --context="$CTX"
```

Restart the broker to pick up the new server:

```bash
oc rollout restart deploy/mcp-gateway -n mcp-system --context="$CTX"
```

## Step 4: Configure Keycloak Tool Permissions

Create a bearer-only Keycloak client with client roles for each tool, and assign all roles to both workshop users:

```bash
bash setup-calculus-roles.sh
```

This creates:
- A bearer-only client `mcp-ecosystem/calculus-helper` (role container, no login)
- 8 client roles: `calculate_area_under_curve`, `compute_derivative`, `compute_integral`, `compute_limit`, `expand_series`, `multivariable_calc`, `solve_equation`, `solve_ode`
- Assigns all 8 roles to both `developer-a` and `developer-b`

## Step 5: Update VirtualMCPServers

Add the new calculus tools (unprefixed) to both VirtualMCPServer resources so they appear in `tools/list`:

```bash
# Add to admin-tools
oc patch mcpvirtualserver admin-tools -n mcp-system --context="$CTX" --type=json \
  -p '[{"op":"add","path":"/spec/tools/-","value":"calculate_area_under_curve"},
       {"op":"add","path":"/spec/tools/-","value":"compute_derivative"},
       {"op":"add","path":"/spec/tools/-","value":"compute_integral"},
       {"op":"add","path":"/spec/tools/-","value":"compute_limit"},
       {"op":"add","path":"/spec/tools/-","value":"expand_series"},
       {"op":"add","path":"/spec/tools/-","value":"multivariable_calc"},
       {"op":"add","path":"/spec/tools/-","value":"solve_equation"},
       {"op":"add","path":"/spec/tools/-","value":"solve_ode"}]'

# Add the same to user-tools
oc patch mcpvirtualserver user-tools -n mcp-system --context="$CTX" --type=json \
  -p '[{"op":"add","path":"/spec/tools/-","value":"calculate_area_under_curve"},
       {"op":"add","path":"/spec/tools/-","value":"compute_derivative"},
       {"op":"add","path":"/spec/tools/-","value":"compute_integral"},
       {"op":"add","path":"/spec/tools/-","value":"compute_limit"},
       {"op":"add","path":"/spec/tools/-","value":"expand_series"},
       {"op":"add","path":"/spec/tools/-","value":"multivariable_calc"},
       {"op":"add","path":"/spec/tools/-","value":"solve_equation"},
       {"op":"add","path":"/spec/tools/-","value":"solve_ode"}]'
```

Restart the broker again:

```bash
oc rollout restart deploy/mcp-gateway -n mcp-system --context="$CTX"
```

## Step 6: Verify Full Access

Get tokens and check `tools/list` for both users:

```bash
# Get a token for developer-a
TOKEN=$(bash scripts/get-mcp-token.sh)

# Check tools/list — should now include the calculus tools
# developer-a: 47 tools (14 OpenShift + 25 GitHub + 8 calculus)
# developer-b: 31 tools (8 OpenShift + 15 GitHub + 8 calculus)
```

## Step 7: Restrict `expand_series` to Admins Only

Now demonstrate per-tool restriction without touching the AuthPolicy.

### 7a: Remove the role from developer-b in Keycloak

```bash
# Get Keycloak admin token
ADMIN_USER=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" -o jsonpath='{.data.username}' | base64 -d)
ADMIN_PASS=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" -o jsonpath='{.data.password}' | base64 -d)
ADMIN_TOKEN=$(curl -sk "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli&username=${ADMIN_USER}&password=${ADMIN_PASS}&grant_type=password" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Look up IDs
CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-ecosystem/calculus-helper" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

USER_B_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users?username=developer-b" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

ROLE_JSON=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/roles/expand_series")

# Remove the role from developer-b
curl -sk -X DELETE -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users/${USER_B_ID}/role-mappings/clients/${CLIENT_UUID}" \
  -d "[${ROLE_JSON}]"
```

### 7b: Remove from the user-tools VirtualMCPServer

```bash
# Find the index of expand_series in user-tools
IDX=$(oc get mcpvirtualserver user-tools -n mcp-system --context="$CTX" \
  -o json | python3 -c "
import json, sys
tools = json.load(sys.stdin)['spec']['tools']
print(tools.index('expand_series'))
")

oc patch mcpvirtualserver user-tools -n mcp-system --context="$CTX" --type=json \
  -p "[{\"op\":\"remove\",\"path\":\"/spec/tools/${IDX}\"}]"
```

### 7c: Verify the restriction

```bash
# developer-b tools/list: expand_series should be gone (30 tools, not 31)
# developer-b tools/call expand_series: should return 403 / PERMISSION_DENIED
# developer-b tools/call compute_derivative: should still work
# developer-a: unchanged — all 47 tools, can call expand_series
```

## What You Learned

- Adding a credential-less MCP server requires **no AuthPolicy changes** — the backend-plane default covers its route and the generic Rego reads `resource_access` dynamically; servers needing backend credentials add their own per-route policy
- Tool permissions are managed in **Keycloak** as client roles on bearer-only clients
- **VirtualMCPServers** control what users see in `tools/list` (discovery)
- **Keycloak client roles** control what users can call via `tools/call` (enforcement)
- Restricting a single tool = remove one Keycloak role + remove from VirtualMCPServer

---

## Workshop Wrap-Up

You have reached the end of the workshop. For a visual recap of everything
you built — the per-user auth/token flow and the complete end-state
architecture (gateway planes, identity stack, Vault, model track,
observability) — see the
**[Architecture Diagrams](../architecture-diagrams.html)** (Reference
section of the site nav). They make a good closing walkthrough and a
take-home reference for bringing this design to your own clusters.

---

**Previous**: [Module 19 -- Observability](../19-observability/README.md)
