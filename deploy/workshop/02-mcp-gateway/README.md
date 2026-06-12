# Module 2: MCP Gateway

This module installs the MCP Gateway operator and the MCP Lifecycle Operator,
then creates a Gateway with a **two-plane topology** and the MCP broker
extension. By the end you will have a working MCP Gateway with a single
public client URL, ready to accept server registrations.

**Prerequisites** -- Module 1 completed. A GatewayClass exists in the cluster.

> **Working directory:**
>
> ```bash
> cd deploy/workshop/02-mcp-gateway
> ```

---

## The Two-Plane Topology

The Gateway carries two kinds of traffic, and keeping them on separate
listeners is what makes per-user identity possible later (Module 8):

- **`mcp` (client plane)** — where developers' MCP clients (Claude Code,
  agents, the Playground) enter, via one public hostname:
  `mcp-gateway.<CLUSTER_DOMAIN>`. The gateway-level AuthPolicy will attach
  here.
- **`mcps` (backend plane)** — used only by the broker's hairpin traffic to
  backend MCP servers, which attach HTTPRoutes with internal `*.mcp.local`
  hostnames. Per-server AuthPolicies attach to those routes.

Both listeners share port 8080 — Envoy separates them by Host header.
Client TLS terminates at an OpenShift Route (edge), so all client traffic
passes the `mcp` listener and its policy. There is deliberately no separate
HTTPS listener: a listener without a policy would be an authentication
bypass.

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

This installs the MCP Gateway operator — currently `mcp-gateway.v0.7.0` (the tech-preview catalog ships only the latest version; expect the minor version to advance over time).

!!! warning "InstallPlan May Require Approval"

    On some clusters, OLM bundles the install plan with dependencies from
    other operators and sets it to Manual approval -- even when the
    subscription specifies Automatic. If the CSV doesn't appear after
    2--3 minutes, check for pending InstallPlans:

    ```bash
    oc get installplan -n openshift-operators
    ```

    If you see any with `APPROVED=false`, approve them — **except** the
    parked RHCL upgrade plan (the workshop pins RHCL to 1.3.x; see
    Module 1). The loop below skips it:

    ```bash
    for plan in $(oc get installplan -n openshift-operators -o jsonpath='{.items[?(@.spec.approved==false)].metadata.name}'); do
      if oc get installplan "$plan" -n openshift-operators -o jsonpath='{.spec.clusterServiceVersionNames}' | grep -q 'rhcl-operator.v1.4'; then
        echo "skipping $plan (parked RHCL upgrade — pinned to 1.3.x)"; continue
      fi
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

!!! important "REQUIRED: Patch the Lifecycle Operator Memory Limits"

    This is a required step, not conditional troubleshooting. The
    lifecycle operator ships with a 128Mi memory limit and gets
    `OOMKilled` on real clusters. Patch to 512Mi:

```bash
oc patch deployment -n mcp-lifecycle-operator-system \
  $(oc get deployment -n mcp-lifecycle-operator-system -o jsonpath='{.items[0].metadata.name}') \
  --type=json -p '[
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value": "512Mi"},
    {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "256Mi"}
  ]'
```

> **Operators OOMKilling is a pattern, not a one-off.** The Kuadrant
> operator (`kuadrant-operator-controller-manager` in `openshift-operators`)
> has also been observed CrashLooping at its default 300Mi limit. If
> AuthPolicies later show `Accepted` but never reach `Enforced`, check that
> operator first and raise its limits the same way.

## Step 3: Create the mcp-system Namespace

```bash
oc apply -f mcp-system-namespace.yaml
```

## Step 4: Discover Your Cluster Domain

Several later steps substitute your cluster's apps domain into manifests.
Capture it once:

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
echo "$CLUSTER_DOMAIN"
```

> Commands in this module use your current kube context. If you work with
> multiple clusters, append `--context="$CTX"` to each `oc` command.

## Step 5: Create the Gateway (Two Listeners)

```bash
sed "s/<CLUSTER_DOMAIN>/${CLUSTER_DOMAIN}/g" mcp-gateway-cr.yaml | oc apply -f -
```

This creates the `mcp` (client) and `mcps` (backend) listeners described
above.

!!! important "Single-Level Subdomain Pattern"

    The client hostname is `mcp-gateway.<CLUSTER_DOMAIN>` — a single-level
    subdomain — because the OpenShift wildcard TLS certificate only covers
    `*.apps.cluster-xxx`. Multi-level subdomains (e.g.,
    `mcp-gateway.mcp.apps...`) cause TLS verification failures in clients
    that verify certificates, including Claude Code. Backend routes don't
    have this problem because `*.mcp.local` hostnames are mesh-internal
    and never see client TLS.

## Step 6: Create the MCPGatewayExtension

The MCPGatewayExtension deploys the MCP broker and attaches it to the
Gateway's `mcps` listener:

```bash
oc apply -f mcp-gateway-extension.yaml
```

!!! important "REQUIRED on OpenShift: Set privateHost"

    This is a required step on every OpenShift cluster using
    `data-science-gateway-class`, not conditional troubleshooting. The MCP
    broker resolves the Istio gateway service by appending `-istio` to the
    Gateway name; with this GatewayClass the lookup fails and `tools/call`
    returns DNS errors. Set `privateHost` explicitly:

First, find the Istio gateway service name:

```bash
oc get svc -n mcp-system
```

The Istio gateway service is named `mcp-gateway-<gatewayclass-name>` —
e.g., `mcp-gateway-data-science-gateway-class`. Set it as the privateHost:

```bash
oc patch mcpgatewayextension mcp-gateway -n mcp-system --type=merge -p '
  {"spec": {"privateHost": "mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080"}}'
```

## Step 7: Create the Client Entry Point

Expose the client plane with an edge-TLS Route and attach the client
hostname to the broker:

```bash
sed "s/<CLUSTER_DOMAIN>/${CLUSTER_DOMAIN}/g" route-client-entry.yaml | oc apply -f -
```

This creates two resources:

- An OpenShift **Route** (`mcp-gateway`) at
  `https://mcp-gateway.<CLUSTER_DOMAIN>`, pointing at the **Istio gateway
  service** — never the broker service directly. Traffic that bypasses
  Envoy bypasses ext_proc and every AuthPolicy, and `tools/call` only works
  through ext_proc routing.
- An **HTTPRoute** (`mcp-gateway-client`) attaching the client hostname to
  the `mcp` listener, sending `/mcp` and `/.well-known` to the broker.

This URL is the single gateway endpoint used by **every** consumer for the
rest of the workshop — Claude Code, the agent, and the Playground:

```bash
echo "MCP Gateway client URL: https://mcp-gateway.${CLUSTER_DOMAIN}/mcp"
```

## Step 8: Verify

Wait for the Gateway to show both `Accepted` and `Programmed`:

```bash
oc get gateway mcp-gateway -n mcp-system -o jsonpath='{.status.conditions}' | python3 -m json.tool
```

Verify the MCPGatewayExtension is Ready:

```bash
oc get mcpgatewayextension mcp-gateway -n mcp-system -o jsonpath='{.status.conditions}' | python3 -m json.tool
```

Verify the pods (broker `mcp-gateway-*` and Istio gateway
`mcp-gateway-data-science-gateway-class-*`):

```bash
oc get pods -n mcp-system
```

Verify the client URL answers (no auth configured yet, so the broker
responds directly):

```bash
curl -sk -o /dev/null -w "HTTP %{http_code}\n" \
  "https://mcp-gateway.${CLUSTER_DOMAIN}/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}'
# Expected: HTTP 200 (will become 401-without-token after Module 8)
```

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| MCP Gateway Operator | openshift-operators | Provides MCPGatewayExtension, MCPServerRegistration CRDs |
| MCP Lifecycle Operator | mcp-lifecycle-operator-system | Manages MCPServer CRs |
| Gateway (mcp-gateway) | mcp-system | Two listeners: `mcp` (clients), `mcps` (backend hairpin) |
| MCPGatewayExtension | mcp-system | Deploys the MCP broker on the `mcps` listener |
| Route + client HTTPRoute | mcp-system | `https://mcp-gateway.<CLUSTER_DOMAIN>/mcp` — the one client URL |

> **If a pod later stops receiving traffic after a restart** (requests 504
> at exactly 10s — the ext_proc timeout — and ztunnel logs on the
> destination node show `connection failed: deadline has elapsed`), the
> pod's ambient mesh enrollment is stale. Delete the pod to re-enroll it.

---

**Next**: [Module 3 -- MCP Server Prerequisites](../03-mcp-server-prerequisites/README.md)
