# Module 3: Deploy an MCP Server

This module deploys the OpenShift MCP server -- a read-only MCP server that
exposes Kubernetes API operations as tools. You will encounter a known issue
with the built-in catalog image and learn how to work around it.

**Prerequisites** -- Module 2 completed. The MCP Lifecycle Operator is running.

---

## Step 1: (Optional) Deploy via the RHOAI Dashboard

If you want to see the dashboard experience first:

1. Open the RHOAI dashboard
2. Navigate to **MCP Catalog** in the left sidebar
3. Find **OpenShift MCP Server** and click **Deploy**
4. Choose a namespace (e.g., `mcp-ecosystem`) and click **Deploy**

At this point you will see a failure: the MCPServer CR is created but the
Deployment never becomes ready. The lifecycle operator logs show
"Failed to create Deployment" errors. This is expected -- continue to Step 2.

If you prefer to skip the dashboard and deploy directly, start at Step 2.

## Step 2: Create the mcp-ecosystem Namespace

Create a namespace for MCP server workloads. The Istio sidecar injection label
is included so that the service mesh can manage traffic:

```bash
oc apply -f mcp-ecosystem-namespace.yaml
```

## Step 3: Understand the Known Issues

There are two problems with the catalog-based deployment:

1. **Unpublished image tag** -- The built-in catalog references
   `registry.redhat.io/openshift-mcp-beta/openshift-mcp-server-rhel9:0.2`.
   At the time of this workshop, that image tag has not yet been published to
   the registry. You will use the upstream Quay image as the working
   reference.

2. **Missing prerequisites** -- The RHOAI dashboard does not create the
   ServiceAccount, ClusterRoleBinding, or ConfigMap that the MCPServer CR
   requires. The lifecycle operator validates that these exist but does not
   create them.

## Step 4: Apply the Prerequisites

The OpenShift MCP server needs:

- A **ServiceAccount** (`mcp-viewer`) for Kubernetes API access
- A **ClusterRoleBinding** granting the `view` ClusterRole to that SA
- A **ConfigMap** with the server configuration (read-only mode, denied
  resources, toolsets)

Apply them all at once:

```bash
oc apply -f openshift-mcp-prerequisites.yaml
```

Review the ConfigMap to understand what the server is configured to do:

```bash
oc get configmap openshift-mcp-server-config -n mcp-ecosystem -o yaml
```

Key settings:

- `read_only = true` -- no create/update/delete operations
- `disable_destructive = true` -- destructive tools are disabled
- `toolsets = ["core", "config"]` -- includes pod, node, namespace, and config tools
- `denied_resources` -- Secrets are denied to prevent credential exposure

## Step 5: Deploy the MCPServer CR

If you deployed via the dashboard in Step 1, an MCPServer CR already exists but
has the wrong image. Patch it:

```bash
oc patch mcpserver openshift-mcp-server -n mcp-ecosystem --type=merge -p '
  {"spec": {"source": {"containerImage": {"ref": "quay.io/redhat-user-workloads/ocp-mcp-server-tenant/openshift-mcp-server-release-03:latest"}}}}'
```

If you skipped the dashboard, create the MCPServer CR directly. Copy the
reference from `openshift-mcp-server-cr.yaml` in the source repo, or apply:

```bash
cat <<'EOF' | oc apply -f -
apiVersion: mcp.x-k8s.io/v1alpha1
kind: MCPServer
metadata:
  name: openshift-mcp-server
  namespace: mcp-ecosystem
spec:
  source:
    type: ContainerImage
    containerImage:
      ref: quay.io/redhat-user-workloads/ocp-mcp-server-tenant/openshift-mcp-server-release-03:latest
  config:
    port: 8080
    arguments:
      - --config
      - /etc/mcp-config/config.toml
    storage:
      - path: /etc/mcp-config
        source:
          type: ConfigMap
          configMap:
            name: openshift-mcp-server-config
  runtime:
    security:
      serviceAccountName: mcp-viewer
EOF
```

## Step 6: Verify the Pod is Running

The lifecycle operator creates a Deployment and Service from the MCPServer CR.
Wait for the pod to start:

```bash
oc get pods -n mcp-ecosystem -w
```

You should see `openshift-mcp-server-*` with status `Running`. The server
starts on port 8080.

Verify the service exists:

```bash
oc get svc -n mcp-ecosystem
```

You can test the MCP server directly (bypassing the gateway) to confirm it
works. Send an `initialize` request, which returns the server's capabilities
including the list of supported methods:

> **Note:** The MCP streamable-http protocol requires an `initialize`
> call before `tools/list`. For a quick verification, `initialize` is
> sufficient -- it confirms the server is running and responding to MCP
> requests.

```bash
oc exec -n mcp-ecosystem deploy/openshift-mcp-server -- curl -s http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}' \
  | python3 -m json.tool
```

You should see a response with `serverInfo` and `capabilities` including
`tools` -- confirming the server is ready.

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| mcp-ecosystem Namespace | -- | Workload namespace for MCP servers |
| ServiceAccount (mcp-viewer) | mcp-ecosystem | K8s API identity for the MCP server |
| ClusterRoleBinding | cluster-scoped | Grants `view` role to mcp-viewer SA |
| ConfigMap | mcp-ecosystem | Server config: read-only, denied resources |
| MCPServer CR | mcp-ecosystem | Tells the lifecycle operator what to deploy |
