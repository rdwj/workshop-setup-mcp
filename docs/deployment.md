# MCP Demo Stack Deployment

Full-stack deployment of the MCP demo: agent, gateway, and chat UI on OpenShift.

## Architecture

```
Browser --> UI (port 3000) --> Gateway (port 8080) --> Agent (port 8080) --> MCP Server
                                                         |
                                                    LLM endpoint (vLLM)
```

| Component | Project | Description |
|-----------|---------|-------------|
| Agent | `workshop-setup-mcp` | Python/fipsagents AI agent with MCP tool integration |
| Gateway | `workshop-setup-mcp-gateway` | Go HTTP gateway (auth, file upload, routing) |
| UI | `workshop-setup-mcp-ui` | Go chat UI with streaming support |

## Prerequisites

- OpenShift cluster with `oc` and `helm` CLI tools
- A deployed LLM (vLLM-compatible endpoint, e.g. `gpt-oss-20b`)
- A deployed MCP server (e.g. `openshift-mcp` in `mcp-ecosystem`)
- Container images built (see Build section below)

## Build

All three components use OpenShift BuildConfig for server-side builds.
Set the context and namespace variables first:

```bash
CTX="<your-kube-context>"
NS="workshop-setup-mcp"
oc create namespace "$NS" --context="$CTX" 2>/dev/null || true
```

### Agent

```bash
cd workshop-setup-mcp

# Create BuildConfig (first time only)
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

# Build
oc start-build workshop-setup-mcp --from-dir=. -n "$NS" --context="$CTX" --follow
```

### Gateway

```bash
cd workshop-setup-mcp-gateway
sed 's/PLACEHOLDER/workshop-setup-mcp-gateway/g' build/buildconfig.yaml \
  | oc apply -n "$NS" --context="$CTX" -f -
oc start-build workshop-setup-mcp-gateway --from-dir=. -n "$NS" --context="$CTX" --follow
```

### UI

```bash
cd workshop-setup-mcp-ui
sed 's/PLACEHOLDER/workshop-setup-mcp-ui/g' build/buildconfig.yaml \
  | oc apply -n "$NS" --context="$CTX" -f -
oc start-build workshop-setup-mcp-ui --from-dir=. -n "$NS" --context="$CTX" --follow
```

## Deploy

Deploy order: agent first, then gateway, then UI.

### Agent

The agent reads configuration from environment variables injected via a ConfigMap.
Key variables (see `agent.yaml` for the full list):

| Variable | Description | Example |
|----------|-------------|---------|
| `MODEL_ENDPOINT` | vLLM-compatible LLM endpoint | `http://my-model-predictor.ns.svc.cluster.local:8443/v1` |
| `MODEL_NAME` | Model identifier | `redhataigpt-oss-20b` |
| `MCP_GATEWAY_URL` | MCP server or gateway URL | `http://openshift-mcp.mcp-ecosystem.svc.cluster.local:8080/mcp` |
| `OPENAI_API_KEY` | Required by the openai SDK (any non-empty value for unauthenticated vLLM) | `not-required` |
| `MCP_AUTH_TOKEN` | JWT for authenticated MCP gateway (empty for direct MCP) | |

```bash
AGENT_IMAGE=$(oc get is workshop-setup-mcp -n "$NS" --context="$CTX" \
  -o jsonpath='{.status.dockerImageRepository}')

helm upgrade --install workshop-setup-mcp chart/ \
  -n "$NS" --kube-context="$CTX" \
  --set image.repository="$AGENT_IMAGE" \
  --set image.tag=latest \
  --set config.MODEL_ENDPOINT="<your-model-endpoint>" \
  --set config.MODEL_NAME="<your-model-name>" \
  --set config.MCP_GATEWAY_URL="<your-mcp-url>" \
  --set config.OPENAI_API_KEY=not-required \
  --set route.enabled=false \
  --wait
```

### Gateway

```bash
GW_IMAGE=$(oc get is workshop-setup-mcp-gateway -n "$NS" --context="$CTX" \
  -o jsonpath='{.status.dockerImageRepository}')

helm upgrade --install workshop-setup-mcp-gateway \
  ../workshop-setup-mcp-gateway/chart/ \
  -n "$NS" --kube-context="$CTX" \
  --set image.repository="$GW_IMAGE" \
  --set image.tag=latest \
  --set config.BACKEND_URL=http://workshop-setup-mcp:8080 \
  --set auth.mode=anonymous \
  --set route.enabled=true \
  --wait
```

### UI

```bash
UI_IMAGE=$(oc get is workshop-setup-mcp-ui -n "$NS" --context="$CTX" \
  -o jsonpath='{.status.dockerImageRepository}')

helm upgrade --install workshop-setup-mcp-ui \
  ../workshop-setup-mcp-ui/chart/ \
  -n "$NS" --kube-context="$CTX" \
  --set image.repository="$UI_IMAGE" \
  --set image.tag=latest \
  --set config.API_URL=http://workshop-setup-mcp-gateway:8080 \
  --set route.enabled=true \
  --wait
```

## Model Endpoint Configuration

### Cluster-internal model (recommended for demos)

Use the Kubernetes service DNS name of your vLLM InferenceService:

```
MODEL_ENDPOINT=http://<predictor-svc>.<model-namespace>.svc.cluster.local:<port>/v1
```

The port is typically 8443 for RHOAI KServe predictors or 8080 for standalone vLLM.

### External model

Use the OpenShift Route URL. If the route has TLS edge termination and no
token auth, the agent can call it directly:

```
MODEL_ENDPOINT=https://<route-host>/v1
```

If the route requires authentication, set `OPENAI_API_KEY` to the bearer token.

## MCP Server Configuration

### Direct MCP (no auth, simplest for demos)

Point the agent directly at the MCP server's ClusterIP service:

```
MCP_GATEWAY_URL=http://openshift-mcp.<mcp-namespace>.svc.cluster.local:8080/mcp
MCP_AUTH_TOKEN=
```

### Authenticated MCP Gateway (Kuadrant/Keycloak)

Point the agent at the MCP Gateway service and provide a JWT:

```
MCP_GATEWAY_URL=http://mcp-gateway.<mcp-namespace>.svc.cluster.local:8080/mcp
MCP_AUTH_TOKEN=<jwt-from-keycloak>
```

To obtain a JWT from Keycloak:

```bash
curl -s -X POST \
  "https://<keycloak-host>/realms/<realm>/protocol/openid-connect/token" \
  -d "grant_type=client_credentials" \
  -d "client_id=<client-id>" \
  -d "client_secret=<client-secret>" \
  | jq -r .access_token
```

Note: Keycloak tokens expire (default 5 minutes). For long-running demos,
either increase the token lifetime in Keycloak or use the direct MCP approach.

## Route Timeouts

Agent tool-calling chains can take 30-60+ seconds. The default OpenShift
Route timeout of 30s will cause 504 errors. Always set a 300s timeout:

```bash
oc annotate route workshop-setup-mcp-gateway \
  haproxy.router.openshift.io/timeout=300s --overwrite \
  -n "$NS" --context="$CTX"

oc annotate route workshop-setup-mcp-ui \
  haproxy.router.openshift.io/timeout=300s --overwrite \
  -n "$NS" --context="$CTX"
```

## Verification

```bash
# All pods should be Running
oc get pods -n "$NS" --context="$CTX" | grep -v build

# Agent should show "Connected to MCP server" and tool count
oc logs deployment/workshop-setup-mcp -n "$NS" --context="$CTX" --tail=20

# Get the UI URL
oc get route workshop-setup-mcp-ui -n "$NS" --context="$CTX" \
  -o jsonpath='https://{.spec.host}{"\n"}'
```

## Teardown

```bash
helm uninstall workshop-setup-mcp-ui -n "$NS" --kube-context="$CTX"
helm uninstall workshop-setup-mcp-gateway -n "$NS" --kube-context="$CTX"
helm uninstall workshop-setup-mcp -n "$NS" --kube-context="$CTX"
oc delete bc,is --all -n "$NS" --context="$CTX"
```
