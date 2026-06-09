# Module 12: Agent Testing Through the Gateway

Observe how identity-based tool filtering changes what the agent can do.
The agent deployed in Module 11 is already connected to the MCP Gateway
with admin credentials. You will verify admin access, then switch to
user-level credentials to see the tool set shrink.

**Time:** 15--20 minutes

**Prerequisites:**
- Module 11 complete (agent, gateway proxy, and chat UI deployed with admin credentials)
- Module 10 complete (Keycloak + AuthPolicy deployed)

> **Working directory:**
>
> ```bash
> cd deploy/workshop/12-agent-test
> ```

## Variables

```bash
CTX="<your-kube-context>"
NS="workshop-setup-mcp"
CLUSTER_DOMAIN=$(oc get ingress.config cluster --context="$CTX" -o jsonpath='{.spec.domain}')
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" -o jsonpath='{.spec.host}')"
```

---

## Step 1: Verify Admin Access

The agent was deployed in Module 11 with admin credentials (`mcp-gateway`
client, member of `mcp-admins`). Confirm the agent is connected to the
gateway with 14 tools:

```bash
oc logs deployment/workshop-setup-mcp -n "$NS" --context="$CTX" | grep "tool(s)"
```

You should see `14 tool(s)` -- the full admin tool set.

Get the chat UI URL:

```bash
UI_URL="https://$(oc get route workshop-setup-mcp-ui -n "$NS" \
  --context="$CTX" -o jsonpath='{.spec.host}')"
echo "Chat UI: ${UI_URL}"
```

Open the URL in your browser.

## Step 2: Test Admin Access Through the Gateway

Refresh the chat UI and ask:

> List all projects

The agent should still return results. It now obtains a Keycloak JWT with the
`mcp-admins` group, presents it to the gateway, and gets the full 14-tool
wristband.

Now try something that requires cluster-level tools:

> Show me the top nodes by CPU usage

This should work -- `nodes_top` is in the admin tool set.

## Step 3: Observe Write Restrictions

The MCP server backing this gateway only provides read-only Kubernetes tools.
Ask the agent:

> Create a new project called test-project

The model should explain that it doesn't have a tool for creating projects.
This is by design -- the OpenShift MCP server exposes `get` and `list`
operations, not `create` or `delete`.

## Step 4: Switch to User-Level Access

Get a Keycloak admin token, then create a `mcp-user-agent` client that
belongs to `mcp-users` instead of `mcp-admins`:

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
```

Create the user-level client:

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

## Step 5: Observe Reduced Tool Access

Refresh the chat UI and ask:

> List all projects

This still works -- `projects_list` is in both the admin and user tool
sets.

Now ask:

> Show me the top nodes by CPU usage

The agent will attempt to call `nodes_top`. With user credentials, the
wristband's `allowed-tools` claim contains only the 8 user-level
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

---

**Next**: [Module 13 -- Gen AI Playground](../13-playground/README.md)
