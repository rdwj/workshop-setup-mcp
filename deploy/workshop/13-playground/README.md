# Module 13: Gen AI Playground with MCP Tools

This module connects the RHOAI Gen AI Studio Playground to the MCP Gateway deployed in earlier modules. The Playground uses a LlamaStack Distribution (LSD) backend that connects to both an external model and the MCP Gateway for tool calling. Students bring their own OpenAI-compatible model endpoint.

**Time:** 30--45 minutes

**Prerequisites:**

- Modules 0--10 complete (MCP Gateway with authentication)
- An OpenAI-compatible model endpoint that supports tool calling (must support `--enable-auto-tool-choice` or equivalent)
- The model's API key (if required)

> **Working directory:** `cd deploy/workshop/13-playground`

## Variables

```bash
CTX="<your-kube-context>"
NS="workshop-setup-mcp"
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster --context="$CTX" -o jsonpath='{.spec.domain}')
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" -o jsonpath='{.spec.host}')"

# Your external model -- students provide these
MODEL_URL="<your-openai-compatible-endpoint>/v1"   # must end with /v1
MODEL_NAME="<your-model-id>"                        # exact model ID from /v1/models
API_KEY="<your-api-key>"                            # or "not-required" if no auth

# Discover the Istio gateway service name (same approach as Module 6)
oc get svc -n mcp-system --context="$CTX"
MCP_GATEWAY_SVC="<istio-gateway-service-name>"      # e.g. mcp-gateway-data-science-gateway-class
```

!!! note "Istio Gateway Service"

    Look for the service that is NOT the plain `mcp-gateway` broker -- it will be
    named `mcp-gateway-<gatewayclass-name>`. This is the Istio gateway service that
    handles MCP traffic routing.

## Step 1: Enable the LlamaStack Operator

Patch the DataScienceCluster to enable the LlamaStack operator:

```bash
oc patch dsc default-dsc --context="$CTX" --type=merge -p '{
  "spec": {
    "components": {
      "llamastackoperator": {"managementState": "Managed"}
    }
  }
}'
```

Wait for DSC to return to Ready (can take 2--5 minutes):

```bash
oc get dsc default-dsc --context="$CTX" -o jsonpath='{.status.conditions}' \
  | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    if c['type'] in ('Ready', 'LlamaStackOperatorReady'):
        print(f\"{c['type']}: {c['status']}\")
"
```

Both should show `True`.

Verify LlamaStack CRDs are registered:

```bash
oc get crd --context="$CTX" | grep llamastack
```

You should see `llamastackdistributions.llamastack.io` (and possibly others).

## Step 2: Deploy MinIO

MinIO provides S3 storage for Gen AI Studio features like prompt management.

```bash
oc apply -f minio.yaml --context="$CTX"
```

Wait for the deployment:

```bash
oc get deployment minio -n minio --context="$CTX" -w
```

Get the console URL:

```bash
echo "MinIO console: https://$(oc get route minio-console -n minio --context="$CTX" -o jsonpath='{.spec.host}')"
```

Credentials: `minioadmin` / `workshop-minio-2024`

## Step 3: Verify the External Model

Before configuring the Playground, confirm your model endpoint is reachable and supports tool calling.

List available models:

```bash
curl -s -H "Authorization: Bearer ${API_KEY}" "${MODEL_URL}/models" | python3 -m json.tool
```

Note the exact model ID in the `id` field -- this must match `MODEL_NAME`.

Test tool calling:

```bash
curl -s "${MODEL_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d '{
    "model": "'"${MODEL_NAME}"'",
    "messages": [{"role": "user", "content": "Hello"}],
    "tools": [{"type": "function", "function": {
      "name": "test_tool", "description": "A test tool",
      "parameters": {"type": "object", "properties": {}}
    }}],
    "max_tokens": 50
  }'
```

!!! warning "Model Must Support Tool Calling"

    The model must support tool calling (e.g., vLLM with `--enable-auto-tool-choice`
    and `--tool-call-parser`). Without this, MCP tools will appear in the Playground
    but the model will never invoke them. Not all models support tool calling --
    for example, Granite 3.3 8B writes Python code instead of generating proper
    tool calls.

## Step 4: Create the Data Science Project

Create a namespace that appears as a Data Science Project in the RHOAI dashboard:

```bash
oc apply -f project-namespace.yaml --context="$CTX"
```

## Step 5: Register the External Model Endpoint

Create the API key secret and model endpoint ConfigMap. Substitute your model details:

```bash
sed "s|<API_KEY>|${API_KEY}|g" endpoint-secret.yaml \
  | oc apply --context="$CTX" -f -
```

```bash
sed -e "s|<MODEL_URL>|${MODEL_URL}|g" \
    -e "s|<MODEL_NAME>|${MODEL_NAME}|g" \
    custom-model-endpoints.yaml \
  | oc apply --context="$CTX" -f -
```

!!! note "ConfigMap Format"

    The `gen-ai-aa-custom-model-endpoints` ConfigMap format is specific to the
    RHOAI Gen AI Studio BFF. Key requirements:

    - `provider_type` must be `remote::openai` for OpenAI-compatible endpoints
    - `base_url` must include the `/v1` path suffix (the LSD appends `/chat/completions`)
    - `allowed_models` must exactly match the model ID from `/v1/models`
    - `provider_id` must match between the provider and the registered model

## Step 6: Register MCP Gateway with the Playground

Register the MCP Gateway so the Playground can discover and call MCP tools. The ConfigMap goes in `redhat-ods-applications` because the Gen AI BFF reads MCP server configuration cluster-wide from that namespace.

```bash
sed "s/<MCP_GATEWAY_SVC>/${MCP_GATEWAY_SVC}/g" mcp-servers-configmap.yaml \
  | oc apply --context="$CTX" -f -
```

!!! important "Internal URL Required"

    The MCP servers ConfigMap must use the internal ClusterIP URL
    (`http://<service>.mcp-system.svc.cluster.local:8080/mcp`), not the external
    Route URL. The Gen AI BFF connects to MCP servers server-side from within
    the cluster. Use the Istio gateway service -- not the broker service. The
    broker responds to `tools/list` from its cache, but `tools/call` only works
    through the Istio gateway's ext_proc routing.

## Step 7: Use the Playground

Walk through the UI steps:

1. Open the RHOAI dashboard:
   ```bash
   echo "Dashboard: https://rhods-dashboard-redhat-ods-applications.${CLUSTER_DOMAIN}"
   ```

2. Navigate to **Gen AI Studio** > **AI asset endpoints**

3. The external model should appear in the Models list. If it doesn't, wait 30--60 seconds for the dashboard to refresh.

4. Click **Add to Playground** next to the model

5. Go to **Gen AI Studio** > **Playground**

6. Select the model from the Model dropdown

Then enable MCP tools:

1. In the Playground settings panel, click the **MCP** tab
2. Check the box next to **MCP-Gateway-Tools**
3. Click the **Auth** (lock) icon to enter a Keycloak token

Get a token:

A helper script is provided for convenience. Run it to get a token:

```bash
./get-mcp-token.sh "$CTX"
```

To copy the token directly to your clipboard (macOS):

```bash
./get-mcp-token.sh "$CTX" | pbcopy
```

The full token acquisition process is shown below for reference:

```bash
# Get the mcp-gateway client secret from Keycloak
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

CLIENT_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])" \
  | xargs -I{} curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/{}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

MCP_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-gateway" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "grant_type=client_credentials" \
  -d "scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "$MCP_TOKEN"
```

4. Paste the token in the MCP Auth dialog and click **Authorize**. You should see "Connection successful."

5. Click the **View tools** icon to see the available MCP tools (should match what Module 12 showed)

!!! note "Token Expiry"

    MCP auth tokens are session-scoped and stored in the browser only. If you
    close the browser or the token expires, you will need to re-authorize.

Test with prompts:

> List all projects in the cluster

> What pods are running in the mcp-system namespace?

The model should generate tool calls, the Playground executes them through the MCP Gateway, and you see the results inline.

## Step 8: Verify

Verification checklist:

```bash
# LlamaStack operator enabled
oc get dsc default-dsc --context="$CTX" \
  -o jsonpath='{.status.components.llamastackoperator.managementState}'
# Expected: Managed

# MinIO running
oc get deployment minio -n minio --context="$CTX" \
  -o jsonpath='{.status.readyReplicas}'
# Expected: 1

# Data Science Project exists
oc get namespace workshop-playground --context="$CTX" \
  -o jsonpath='{.metadata.labels.opendatahub\.io/dashboard}'
# Expected: true

# External model ConfigMap
oc get configmap gen-ai-aa-custom-model-endpoints \
  -n workshop-playground --context="$CTX"
# Expected: exists

# MCP servers ConfigMap
oc get configmap gen-ai-aa-mcp-servers \
  -n redhat-ods-applications --context="$CTX"
# Expected: exists
```

## What You Deployed

| Resource | Namespace | Purpose |
|----------|-----------|---------|
| LlamaStack operator | (cluster-wide) | Manages LlamaStackDistribution CRs for the Playground backend |
| MinIO | minio | S3 storage for Gen AI Studio features |
| workshop-playground Namespace | -- | Data Science Project for the Playground |
| endpoint-api-key-1 Secret | workshop-playground | API key for the external model endpoint |
| gen-ai-aa-custom-model-endpoints ConfigMap | workshop-playground | External model registration for the Playground |
| gen-ai-aa-mcp-servers ConfigMap | redhat-ods-applications | MCP Gateway registration for the Playground |

---

> **How it works:** When you open the Playground, the Gen AI BFF creates a
> LlamaStackDistribution (LSD) in your project namespace. The LSD connects to
> the external model for inference and to the MCP Gateway for tool calling.
> Chat messages flow: Browser > BFF > LSD > External Model. Tool calls flow:
> LSD > MCP Gateway > Backend MCP Server. Auth tokens entered in the Playground
> are forwarded by the LSD to the MCP Gateway, so the same identity-based tool
> filtering from Module 10 applies.
