# Module 16: Adding a Third-Party MCP Server

This module walks through adding a new MCP server to the gateway ecosystem end-to-end: deploying the server, registering it with the gateway, configuring tool-level permissions in Keycloak, and demonstrating per-tool access restriction.

The key takeaway: **no AuthPolicy changes are needed**. The generic Rego reads tool permissions from Keycloak's `resource_access` JWT claim, so adding servers and managing permissions is done entirely in Keycloak and VirtualMCPServer resources.

## Prerequisites

- Modules 0-10 completed (gateway, auth, Keycloak realm with `resource_access` client roles)
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

The HTTPRoute tells the gateway proxy how to reach the calculus-helper:

```bash
sed "s|<CLUSTER_DOMAIN>|${CLUSTER_DOMAIN}|g" httproute.yaml \
  | oc apply -f - --context="$CTX"
```

## Step 3: Register with the Gateway

The MCPServerRegistration tells the broker about the new server and applies the `calculus_` prefix to all its tools:

```bash
oc apply -f mcpserverregistration.yaml --context="$CTX"
```

Wait for the registration to show `READY=True`:

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

Add the new `calculus_*` tools to both VirtualMCPServer resources so they appear in `tools/list`:

```bash
# Add to admin-tools
oc patch mcpvirtualserver admin-tools -n mcp-system --context="$CTX" --type=json \
  -p '[{"op":"add","path":"/spec/tools/-","value":"calculus_calculate_area_under_curve"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_compute_derivative"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_compute_integral"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_compute_limit"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_expand_series"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_multivariable_calc"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_solve_equation"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_solve_ode"}]'

# Add the same to user-tools
oc patch mcpvirtualserver user-tools -n mcp-system --context="$CTX" --type=json \
  -p '[{"op":"add","path":"/spec/tools/-","value":"calculus_calculate_area_under_curve"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_compute_derivative"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_compute_integral"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_compute_limit"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_expand_series"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_multivariable_calc"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_solve_equation"},
       {"op":"add","path":"/spec/tools/-","value":"calculus_solve_ode"}]'
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

# Check tools/list — should now include calculus_* tools
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
# Find the index of calculus_expand_series in user-tools
IDX=$(oc get mcpvirtualserver user-tools -n mcp-system --context="$CTX" \
  -o json | python3 -c "
import json, sys
tools = json.load(sys.stdin)['spec']['tools']
print(tools.index('calculus_expand_series'))
")

oc patch mcpvirtualserver user-tools -n mcp-system --context="$CTX" --type=json \
  -p "[{\"op\":\"remove\",\"path\":\"/spec/tools/${IDX}\"}]"
```

### 7c: Verify the restriction

```bash
# developer-b tools/list: calculus_expand_series should be gone (30 tools, not 31)
# developer-b tools/call calculus_expand_series: should return 403 / PERMISSION_DENIED
# developer-b tools/call calculus_compute_derivative: should still work
# developer-a: unchanged — all 47 tools, can call expand_series
```

## What You Learned

- Adding a new MCP server requires **no AuthPolicy changes** — the generic Rego reads `resource_access` dynamically
- Tool permissions are managed in **Keycloak** as client roles on bearer-only clients
- **VirtualMCPServers** control what users see in `tools/list` (discovery)
- **Keycloak client roles** control what users can call via `tools/call` (enforcement)
- Restricting a single tool = remove one Keycloak role + remove from VirtualMCPServer

---

**Previous**: [Module 15 -- Observability](../15-observability/README.md)
