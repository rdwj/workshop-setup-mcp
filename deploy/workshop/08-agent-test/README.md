# Module 8: Agent Testing Through the Gateway

Connect a live agent to the MCP Gateway and observe how identity-based tool
filtering changes what the agent can do.

You will reconfigure the agent deployed in Module 7 to route tool calls
through the authenticated MCP Gateway and see the difference between admin
and user-level access.

**Time:** 15--20 minutes

**Prerequisites:**
- Module 7 complete (agent, gateway proxy, and chat UI deployed)
- Module 6 complete (Keycloak + AuthPolicy deployed)

> **Working directory:**
>
> ```bash
> cd deploy/workshop/08-agent-test
> ```

## Variables

```bash
CTX="<your-kube-context>"
CLUSTER_DOMAIN=$(oc get ingress.config cluster --context="$CTX" -o jsonpath='{.spec.domain}')
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" -o jsonpath='{.spec.host}')"
```

---

## Step 1: Verify the Pre-deployed Components

The workshop cluster has an agent stack already running. Confirm the
components are up:

```bash
oc get pods -n workshop-setup-mcp --context="$CTX"
```

Get the chat UI URL:

```bash
UI_URL="https://$(oc get route workshop-setup-mcp-ui -n workshop-setup-mcp \
  --context="$CTX" -o jsonpath='{.spec.host}')"
echo "Chat UI: ${UI_URL}"
```

Open the URL in your browser. The agent is currently connected directly to the
MCP server -- no gateway, no auth.

## Step 2: Test Direct Mode (No Gateway)

In the chat UI, ask:

> List all projects in this cluster

The agent should call the `projects_list` tool and return results. This works
because the agent talks directly to the MCP server using the server's
ServiceAccount token.

Now ask:

> What nodes are in this cluster and what's their CPU usage?

This should also work -- the agent has access to all 14 tools in direct mode.

## Step 3: Reconfigure the Agent for Gateway Access (Admin)

Patch the agent's ConfigMap to route through the MCP Gateway with admin-level
Keycloak credentials.

First, get the `mcp-gateway` client secret from Keycloak:

```bash
ADMIN_USER=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" \
  -o jsonpath='{.data.username}' | base64 -d)
ADMIN_PASS=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" \
  -o jsonpath='{.data.password}' | base64 -d)

ADMIN_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" \
  -d "username=${ADMIN_USER}" \
  -d "password=${ADMIN_PASS}" \
  -d "grant_type=password" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

CLIENT_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

echo "Client secret: ${CLIENT_SECRET}"
```

Update `agent-config-admin.yaml` with your values, then apply:

We use the internal service URL because the agent runs inside the cluster:

```bash
MCP_GATEWAY_URL="http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp"

sed -e "s|<MCP_GATEWAY_URL>|${MCP_GATEWAY_URL}|g" \
    -e "s|<KEYCLOAK_URL>|${KEYCLOAK_URL}|g" \
    -e "s|<CLIENT_SECRET>|${CLIENT_SECRET}|g" \
    agent-config-admin.yaml \
  | oc apply --context="$CTX" -f -
```

Restart the agent to pick up the new config:

```bash
oc rollout restart deployment/workshop-setup-mcp \
  -n workshop-setup-mcp --context="$CTX"
oc rollout status deployment/workshop-setup-mcp \
  -n workshop-setup-mcp --context="$CTX" --timeout=60s
```

## Step 4: Test Admin Access Through the Gateway

Refresh the chat UI and ask:

> List all projects

The agent should still return results. It now obtains a Keycloak JWT with the
`mcp-admins` group, presents it to the gateway, and gets the full 14-tool
wristband.

Now try something that requires cluster-level tools:

> Show me the top nodes by CPU usage

This should work -- `nodes_top` is in the admin tool set.

## Step 5: Observe Write Restrictions

The MCP server backing this gateway only provides read-only Kubernetes tools.
Ask the agent:

> Create a new project called test-project

The model should explain that it doesn't have a tool for creating projects.
This is by design -- the OpenShift MCP server exposes `get` and `list`
operations, not `create` or `delete`.

## Step 6: Switch to User-Level Access

Create a `mcp-user-agent` client in Keycloak that belongs to `mcp-users`
instead of `mcp-admins`:

```bash
# Create the client
curl -sk -o /dev/null -w "HTTP %{http_code}\n" -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients" \
  -d '{"clientId":"mcp-user-agent","enabled":true,"serviceAccountsEnabled":true,"standardFlowEnabled":false,"directAccessGrantsEnabled":false}'

# Get the client UUID
USER_CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-user-agent" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# Assign groups scope
GROUPS_SCOPE_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/client-scopes" \
  | python3 -c "import sys,json; print([s['id'] for s in json.load(sys.stdin) if s['name']=='groups'][0])")

curl -sk -X PUT -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${USER_CLIENT_UUID}/default-client-scopes/${GROUPS_SCOPE_ID}"

# Get the SA user and assign to mcp-users group
SA_USER_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users?username=service-account-mcp-user-agent" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

USERS_GROUP_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/groups" \
  | python3 -c "import sys,json; print([g['id'] for g in json.load(sys.stdin) if g['name']=='mcp-users'][0])")

curl -sk -X PUT -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users/${SA_USER_ID}/groups/${USERS_GROUP_ID}"

# Get the client secret
USER_CLIENT_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${USER_CLIENT_UUID}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

echo "User client secret: ${USER_CLIENT_SECRET}"
```

Patch the agent config with user credentials:

```bash
sed -e "s|<MCP_GATEWAY_URL>|${MCP_GATEWAY_URL}|g" \
    -e "s|<KEYCLOAK_URL>|${KEYCLOAK_URL}|g" \
    -e "s|<CLIENT_SECRET>|${USER_CLIENT_SECRET}|g" \
    agent-config-user.yaml \
  | oc apply --context="$CTX" -f -

oc rollout restart deployment/workshop-setup-mcp \
  -n workshop-setup-mcp --context="$CTX"
oc rollout status deployment/workshop-setup-mcp \
  -n workshop-setup-mcp --context="$CTX" --timeout=60s
```

## Step 7: Observe Reduced Tool Access

Refresh the chat UI and ask:

> List all projects

This still works -- `projects_list` is in the user tool set (8 tools).

Now ask:

> Show me the top nodes by CPU usage

The model should explain it cannot do this. The `nodes_top` tool is not in the
user-level tool set. The gateway's wristband only includes the 8 read-only
tools for non-admin users.

Verify the tool count directly:

```bash
USER_TOKEN=$(curl -sk -X POST \
  "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-user-agent" \
  -d "client_secret=${USER_CLIENT_SECRET}" \
  -d "grant_type=client_credentials" \
  -d "scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Initialize first (required by streamable-http protocol)
SESSION_ID=$(oc exec -n mcp-system deploy/mcp-gateway --context="$CTX" -- \
  curl -sv http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp \
  -H "Host: openshift.mcp.${CLUSTER_DOMAIN}" \
  -H "Authorization: Bearer ${USER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}' \
  2>&1 | grep -i 'mcp-session-id' | head -1 | awk '{print $3}' | tr -d '\r')

# Then tools/list with the session
oc exec -n mcp-system deploy/mcp-gateway --context="$CTX" -- \
  curl -s http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp \
  -H "Host: openshift.mcp.${CLUSTER_DOMAIN}" \
  -H "Authorization: Bearer ${USER_TOKEN}" \
  -H "Mcp-Session-Id: ${SESSION_ID}" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":2}' \
  | python3 -c "
import sys, json
tools = json.load(sys.stdin).get('result', {}).get('tools', [])
print(f'Tool count: {len(tools)}')
for t in tools:
    print(f'  {t[\"name\"]}')
"
```

**Expected:** 8 tools.

---

## Discussion: What the Gateway Controls

The MCP Gateway provides three layers of control:

1. **Authentication** -- Is this caller who they claim to be? (Keycloak JWT)
2. **Tool filtering** -- Which tools can this caller see and invoke? (OPA Rego + wristband)
3. **Federation** -- Multiple MCP servers behind a single endpoint (MCPServerRegistration)

What the gateway does NOT control:
- **Kubernetes RBAC** -- The MCP server uses its own ServiceAccount for K8s API
  calls. All callers share the same SA permissions. Per-user K8s RBAC would
  require per-user ServiceAccounts or token impersonation.
- **Data-level access** -- The gateway controls which tools are visible, not
  what data each tool returns. A user who can call `pods_list` sees all pods
  the SA can see.
- **Model behavior** -- The gateway controls tool access, not what the model
  says. Prompt injection or model hallucination is outside its scope.

This separation is intentional: the gateway handles identity and tool routing
at the API layer, while Kubernetes RBAC, network policies, and pod security
policies handle resource-level access control. This layered defense model
allows each component to enforce its own scope of responsibility.

---

## What You Deployed

- **Agent ConfigMap (admin)** -- reconfigured the pre-deployed agent to route through the MCP Gateway with `mcp-admins` credentials
- **mcp-user-agent Keycloak client** -- a second service account in the `mcp-users` group for demonstrating reduced tool access
- **Agent ConfigMap (user)** -- switched the agent to user-level credentials to observe tool filtering in action
