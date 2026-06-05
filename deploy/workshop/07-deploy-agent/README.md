# Module 7: Deploy the Agent Stack

Build and deploy the three-component demo stack: an AI agent that calls
MCP tools, a gateway proxy that handles auth and routing, and a chat UI.

```
Browser --> UI (port 3000) --> Gateway (port 8080) --> Agent (port 8080) --> MCP Gateway
                                                         |
                                                    LLM endpoint
```

**Time:** 15--20 minutes

**Prerequisites:**
- Modules 1--6 complete (MCP Gateway, MCP server, Keycloak + AuthPolicy)
- `MODEL_ENDPOINT`, `MODEL_NAME`, and `OPENAI_API_KEY` from Module 0

## Variables

Set these once and use them throughout:

```bash
CTX="<your-kube-context>"
NS="workshop-setup-mcp"
CLUSTER_DOMAIN=$(oc get ingress.config cluster --context="$CTX" -o jsonpath='{.spec.domain}')
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" -o jsonpath='{.spec.host}')"
```

---

## Step 1: Create the Namespace

```bash
oc create namespace "$NS" --context="$CTX" 2>/dev/null || echo "namespace exists"
```

## Step 2: Build the Agent

The agent uses an OpenShift BuildConfig to build from source on the
cluster. This avoids architecture mismatches (Mac ARM vs. cluster x86_64).

```bash
cd demo/agent

# Create the BuildConfig and ImageStream (first time only)
cat <<'EOF' | oc apply -n "$NS" --context="$CTX" -f -
apiVersion: image.openshift.io/v1
kind: ImageStream
metadata:
  name: workshop-setup-mcp
---
apiVersion: build.openshift.io/v1
kind: BuildConfig
metadata:
  name: workshop-setup-mcp
spec:
  output:
    to:
      kind: ImageStreamTag
      name: workshop-setup-mcp:latest
  source:
    type: Binary
  strategy:
    type: Docker
    dockerStrategy:
      dockerfilePath: Containerfile
EOF

# Build (uploads source, builds on cluster)
oc start-build workshop-setup-mcp --from-dir=. -n "$NS" --context="$CTX" --follow
```

This takes 2--3 minutes. You should see the build complete with
"Push successful".

## Step 3: Build the Gateway and UI

These use the same BuildConfig pattern via their Makefiles:

```bash
cd ../../demo/gateway
make build-openshift PROJECT="$NS"
```

```bash
cd ../ui
make build-openshift PROJECT="$NS"
```

Each build takes 1--2 minutes.

Verify all three images exist:

```bash
oc get is -n "$NS" --context="$CTX"
```

You should see `workshop-setup-mcp`, `workshop-setup-mcp-gateway`, and
`workshop-setup-mcp-ui` image streams.

## Step 4: Get the Keycloak Client Secret

The agent needs the `mcp-gateway` client secret to acquire JWTs:

```bash
ADMIN_USER=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" \
  -o jsonpath='{.data.username}' | base64 -d)
ADMIN_PASS=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" \
  -o jsonpath='{.data.password}' | base64 -d)

ADMIN_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli&username=${ADMIN_USER}&password=${ADMIN_PASS}&grant_type=password" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

CLIENT_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

echo "Client secret: ${CLIENT_SECRET}"
```

## Step 5: Deploy the Agent

```bash
cd ../agent

AGENT_IMAGE=$(oc get is workshop-setup-mcp -n "$NS" --context="$CTX" \
  -o jsonpath='{.status.dockerImageRepository}')

MCP_GATEWAY_URL="http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp"

helm upgrade --install workshop-setup-mcp chart/ \
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

## Step 6: Deploy the Gateway Proxy

The gateway proxy sits between the chat UI and the agent, handling auth
token exchange and request routing:

```bash
cd ../gateway

GW_IMAGE=$(oc get is workshop-setup-mcp-gateway -n "$NS" --context="$CTX" \
  -o jsonpath='{.status.dockerImageRepository}')

helm upgrade --install workshop-setup-mcp-gateway chart/ \
  -n "$NS" --kube-context="$CTX" \
  --set image.repository="$GW_IMAGE" \
  --set image.tag=latest \
  --set config.BACKEND_URL=http://workshop-setup-mcp:8080 \
  --set auth.mode=anonymous \
  --set route.enabled=true \
  --wait
```

## Step 7: Deploy the Chat UI

```bash
cd ../ui

UI_IMAGE=$(oc get is workshop-setup-mcp-ui -n "$NS" --context="$CTX" \
  -o jsonpath='{.status.dockerImageRepository}')

helm upgrade --install workshop-setup-mcp-ui chart/ \
  -n "$NS" --kube-context="$CTX" \
  --set image.repository="$UI_IMAGE" \
  --set image.tag=latest \
  --set config.API_URL=http://workshop-setup-mcp-gateway:8080 \
  --set route.enabled=true \
  --wait
```

## Step 8: Set Route Timeouts

Agent tool-calling chains can take 30--60+ seconds. The default OpenShift
Route timeout of 30s will cause 504 errors:

```bash
oc annotate route workshop-setup-mcp-gateway \
  haproxy.router.openshift.io/timeout=300s --overwrite \
  -n "$NS" --context="$CTX"

oc annotate route workshop-setup-mcp-ui \
  haproxy.router.openshift.io/timeout=300s --overwrite \
  -n "$NS" --context="$CTX"
```

## Step 9: Verify

```bash
# All pods should be Running
oc get pods -n "$NS" --context="$CTX" | grep -v build

# Agent should show "Connected to MCP server" and "14 tool(s)"
oc logs deployment/workshop-setup-mcp -n "$NS" --context="$CTX" --tail=20

# Get the chat UI URL
UI_URL="https://$(oc get route workshop-setup-mcp-ui -n "$NS" --context="$CTX" \
  -o jsonpath='{.spec.host}')"
echo "Chat UI: ${UI_URL}"
```

If the agent logs show connection errors or you see "Client is not
connected" errors in the chat UI, the broker may have restarted since the
agent connected. Restart the agent to re-establish the MCP session:

```bash
oc rollout restart deployment/workshop-setup-mcp -n "$NS" --context="$CTX"
```

Open the chat UI in your browser and try a basic question:

> What projects are on this cluster?

The agent should call the MCP tools through the gateway and return results.

---

## What You Deployed

| Component | Image | Purpose |
|-----------|-------|---------|
| Agent | `workshop-setup-mcp` | AI agent with MCP tool integration via fipsagents |
| Gateway Proxy | `workshop-setup-mcp-gateway` | HTTP gateway handling auth and request routing |
| Chat UI | `workshop-setup-mcp-ui` | Browser-based chat interface with streaming |

The agent connects to the MCP Gateway using the `mcp-gateway` Keycloak
client (member of `mcp-admins`). It acquires a JWT on startup and
includes it in MCP requests. The gateway validates the JWT, builds a
wristband with the admin tool set (14 tools), and routes tool calls to
the OpenShift MCP server.

---

**Next**: [Module 8 -- Agent Testing](../08-agent-test/README.md)
