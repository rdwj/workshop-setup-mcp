# Module 3: MCP Gateway

This module installs the MCP Gateway operator and the MCP Lifecycle Operator,
then creates a dedicated Gateway with the MCP broker extension. By the end you
will have a working MCP Gateway ready to accept server registrations.

**Prerequisites** -- Module 2 completed. A GatewayClass exists in the cluster.

> **Working directory:**
>
> ```bash
> cd deploy/workshop/03-mcp-gateway
> ```

---

## Step 1: Install the MCP Gateway Operator

The MCP Gateway operator provides the MCPGatewayExtension,
MCPServerRegistration, and MCPVirtualServer CRDs:

```bash
oc apply -f mcp-gateway-subscription.yaml
```

Wait for the CSV to reach `Succeeded`. This can take 2--3 minutes:

```bash
oc get csv -n openshift-operators | grep mcp-gateway
```

This installs `mcp-gateway.v0.6.0`.

!!! warning "InstallPlan May Require Approval"

    On some clusters, OLM bundles the install plan with dependencies from
    other operators and sets it to Manual approval -- even when the
    subscription specifies Automatic. If the CSV doesn't appear after
    2--3 minutes, check for pending InstallPlans:

    ```bash
    oc get installplan -n openshift-operators
    ```

    If you see any with `APPROVED=false`, approve them:

    ```bash
    for plan in $(oc get installplan -n openshift-operators -o jsonpath='{.items[?(@.spec.approved==false)].metadata.name}'); do
      oc patch installplan "$plan" -n openshift-operators --type=merge -p '{"spec":{"approved":true}}'
    done
    ```

## Step 2: Install the MCP Lifecycle Operator

The MCP Lifecycle Operator manages `MCPServer` CRs -- it turns a container
image reference into a running Deployment + Service. Install it from the
upstream GitHub release:

```bash
oc apply -f https://github.com/kubernetes-sigs/mcp-lifecycle-operator/releases/latest/download/install.yaml
```

Wait for the deployment to become available:

```bash
oc get deployment -n mcp-lifecycle-operator-system
```

!!! important "Lifecycle Operator Memory Limits"

    The lifecycle operator ships with a 128Mi memory limit. It may get
    `OOMKilled` on clusters with multiple MCPServer CRs. If the pod
    restarts repeatedly, increase the limit.

Patch the deployment to 512Mi:

```bash
oc patch deployment -n mcp-lifecycle-operator-system \
  $(oc get deployment -n mcp-lifecycle-operator-system -o jsonpath='{.items[0].metadata.name}') \
  --type=json -p '[
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value": "512Mi"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "256Mi"}
  ]'
```

Verify the pod restarts and stays running:

```bash
oc get pods -n mcp-lifecycle-operator-system -w
```

## Step 3: Create the mcp-system Namespace

```bash
oc apply -f mcp-system-namespace.yaml
```

## Step 4: Discover Your Cluster Domain

The MCP Gateway needs your cluster's apps domain for wildcard hostnames.
Discover it:

```bash
oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}'
```

This returns something like `apps.cluster-abc.example.opentlc.com`. You will
substitute this value for `<CLUSTER_DOMAIN>` in the next step.

## Step 5: Create the MCP Gateway

Substitute your cluster domain and apply in one step:

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
sed "s/<CLUSTER_DOMAIN>/${CLUSTER_DOMAIN}/g" mcp-gateway-cr.yaml | oc apply -f -
```

Alternatively, edit `mcp-gateway-cr.yaml` by hand and replace
`<CLUSTER_DOMAIN>` with your cluster domain, then `oc apply -f mcp-gateway-cr.yaml`.

The Gateway creates two listeners:

- **mcps** (port 8080, HTTP) -- used by the MCP broker for tool routing
- **https** (port 443, HTTPS) -- for TLS-terminated client access

The `mcps` listener handles internal broker-to-server communication (HTTP, no
TLS). The `https` listener provides external client access with TLS
termination.

## Step 6: Create the MCPGatewayExtension

The MCPGatewayExtension deploys the MCP broker and attaches it to the Gateway's
`mcps` listener:

```bash
oc apply -f mcp-gateway-extension.yaml
```

!!! important "Broker Service Name on OpenShift"

    The MCP broker resolves the Istio gateway service by appending `-istio` to
    the Gateway name. On OpenShift, the GatewayClass is named
    `data-science-gateway-class` (not `istio`), so the service lookup fails
    and `tools/call` returns DNS errors. Setting `privateHost` explicitly
    tells the broker where to find the Istio gateway service.

After the MCPGatewayExtension is created, patch it to set the
correct `privateHost`:

First, find the Istio gateway service name:

```bash
oc get svc -n mcp-system
```

!!! note "Service Naming Convention"

    The Istio gateway service is named `mcp-gateway-<gatewayclass-name>`,
    not `*-istio`. On OpenShift with RHOAI, the GatewayClass is
    `data-science-gateway-class`, so the service will be
    `mcp-gateway-data-science-gateway-class`. Look for the service that is
    **not** the plain `mcp-gateway` broker service.

Set the Istio gateway service as the privateHost:

```bash
oc patch mcpgatewayextension mcp-gateway -n mcp-system --type=merge -p '
  {"spec": {"privateHost": "mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080"}}'
```

## Step 7: Verify the Gateway

Wait for the Gateway to show both `Accepted` and `Programmed` conditions:

```bash
oc get gateway mcp-gateway -n mcp-system -o jsonpath='{.status.conditions}' | python3 -m json.tool
```

Both conditions should show `status: "True"`. This may take 1-2 minutes while
Istio provisions the listener.

Also verify the MCPGatewayExtension is Ready:

```bash
oc get mcpgatewayextension mcp-gateway -n mcp-system -o jsonpath='{.status.conditions}' | python3 -m json.tool
```

Finally, verify the pods are running:

```bash
oc get pods -n mcp-system
```

You should see two pods: the broker (`mcp-gateway-*`) and the Istio
gateway (`mcp-gateway-data-science-gateway-class-*`).

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| MCP Gateway Operator | openshift-operators | Provides MCPGatewayExtension, MCPServerRegistration CRDs |
| MCP Lifecycle Operator | mcp-lifecycle-operator-system | Manages MCPServer CRs (image to Deployment) |
| mcp-system Namespace | -- | Home for the MCP Gateway |
| Gateway (mcp-gateway) | mcp-system | Dedicated gateway for MCP traffic |
| MCPGatewayExtension | mcp-system | Deploys the MCP broker on the mcps listener |

> **Which service to use:** After creating the Gateway and MCPGatewayExtension,
> you will see two services in `mcp-system`: the broker (`mcp-gateway`) and the
> Istio gateway (`mcp-gateway-<gatewayclass-name>`, e.g.,
> `mcp-gateway-data-science-gateway-class`). Always give MCP clients the Istio
> gateway service URL — not the broker. The broker handles `tools/list` from
> cache but `tools/call` only works through the Istio gateway's ext_proc routing.
