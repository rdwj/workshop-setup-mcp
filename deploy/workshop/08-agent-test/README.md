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

The agent was deployed in Module 7 with admin credentials (`mcp-gateway`
client, member of `mcp-admins`). If you followed Module 7, the agent is
already configured for admin access and you can skip to Step 4.

If you need to reconfigure the agent for admin access (e.g., after
switching to user credentials in Step 6), re-run the Module 7 helm
install with the admin client secret:

```bash
MCP_GATEWAY_URL="http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp"
NS="workshop-setup-mcp"

AGENT_IMAGE=$(oc get is workshop-setup-mcp -n "$NS" --context="$CTX" \
  -o jsonpath='{.status.dockerImageRepository}')

helm upgrade workshop-setup-mcp ../../demo/agent/chart/ \
  -n "$NS" --kube-context="$CTX" \
  --set image.repository="$AGENT_IMAGE" \
  --set image.tag=latest \
  --set config.MODEL_ENDPOINT="${MODEL_ENDPOINT}" \
  --set config.MODEL_NAME="${MODEL_NAME}" \
  --set config.OPENAI_API_KEY="${OPENAI_API_KEY:-not-required}" \
  --set config.MCP_GATEWAY_URL="${MCP_GATEWAY_URL}" \
  --set config.KEYCLOAK_URL="${KEYCLOAK_URL}" \
  --set config.KEYCLOAK_REALM=mcp-gateway \
  --set config.KEYCLOAK_CLIENT_ID=mcp-gateway \
  --set config.KEYCLOAK_CLIENT_SECRET="${CLIENT_SECRET}" \
  --set route.enabled=false \
  --wait
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

Switch the agent to user credentials via helm upgrade:

```bash
AGENT_IMAGE=$(oc get is workshop-setup-mcp -n "$NS" --context="$CTX" \
  -o jsonpath='{.status.dockerImageRepository}')

helm upgrade workshop-setup-mcp ../../demo/agent/chart/ \
  -n "$NS" --kube-context="$CTX" \
  --set image.repository="$AGENT_IMAGE" \
  --set image.tag=latest \
  --set config.MODEL_ENDPOINT="${MODEL_ENDPOINT}" \
  --set config.MODEL_NAME="${MODEL_NAME}" \
  --set config.OPENAI_API_KEY="${OPENAI_API_KEY:-not-required}" \
  --set config.MCP_GATEWAY_URL="${MCP_GATEWAY_URL}" \
  --set config.KEYCLOAK_URL="${KEYCLOAK_URL}" \
  --set config.KEYCLOAK_REALM=mcp-gateway \
  --set config.KEYCLOAK_CLIENT_ID=mcp-user-agent \
  --set config.KEYCLOAK_CLIENT_SECRET="${USER_CLIENT_SECRET}" \
  --set route.enabled=false \
  --wait
```

## Step 7: Observe Reduced Tool Access

Refresh the chat UI and ask:

> List all projects

This still works -- `projects_list` is in both the admin and user tool
sets.

Now ask:

> Show me the top nodes by CPU usage

The agent will attempt to call `nodes_top`. With user credentials, the
wristband's `allowed-capabilities` claim contains only the 8 user-level
tools. If the gateway's wristband enforcement is active, the broker
rejects the call. If it falls through to the backend, the
ServiceAccount's Kubernetes RBAC blocks it (the `mcp-viewer` SA has
`view` role, which does not include node metrics).

Either way, the user-level agent cannot retrieve node CPU usage --
demonstrating defense-in-depth: gateway tool filtering and Kubernetes
RBAC each independently prevent unauthorized access.

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

- **mcp-user-agent Keycloak client** -- a second service account in the `mcp-users` group for demonstrating reduced tool access
- **Agent (user config)** -- switched the agent to user-level credentials via helm upgrade to observe tool filtering in action
