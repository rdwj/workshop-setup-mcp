# Module 9: Gateway Registration

This module registers the OpenShift MCP server with the MCP Gateway so that
its tools are accessible through a single gateway endpoint. You will also
create VirtualMCPServer resources that expose curated tool subsets for
different user roles.

**Prerequisites** -- Modules 2, 6-8 completed. The OpenShift MCP server pod is
running in `mcp-ecosystem`. The MCP Gateway and broker are running in
`mcp-system`.

> **Working directory:**
>
> ```bash
> cd deploy/workshop/09-gateway-registration
> ```

---

## Step 1: Create the HTTPRoute

The HTTPRoute tells the Gateway how to reach the MCP server. It maps a
hostname to the backend service.

Substitute your cluster domain and apply in one step:

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
sed "s/<CLUSTER_DOMAIN>/${CLUSTER_DOMAIN}/g" httproute.yaml | oc apply -f -
```

Alternatively, edit `httproute.yaml` by hand and replace `<CLUSTER_DOMAIN>`
with your cluster's apps domain, then `oc apply -f httproute.yaml`.

The hostname will be `openshift.mcp.<CLUSTER_DOMAIN>`, routed to the
`openshift-mcp-server` service on port 8080.

## Step 2: Create the ReferenceGrant

The HTTPRoute lives in `mcp-ecosystem` but references the Gateway in
`mcp-system`. Kubernetes Gateway API requires an explicit ReferenceGrant in
the Gateway's namespace to allow this cross-namespace reference:

```bash
oc apply -f referencegrant.yaml
```

## Step 3: Create the MCPServerRegistration

The MCPServerRegistration tells the MCP broker about the backend server and
assigns a `prefix`. All tools from this server will be prefixed with
`openshift_` (e.g., `pods_list` becomes `openshift_pods_list`):

```bash
oc apply -f mcpserverregistration.yaml
```

!!! important "`prefix` is Immutable"

    The `prefix` field cannot be changed after the MCPServerRegistration is
    created — it affects tool routing in the broker's configuration cache.
    If you need a different prefix, delete and recreate the resource.
    Plan your naming convention before applying. Common patterns include
    `<team>_` or `<server>_` prefixes.

!!! important "Broker Does Not Auto-Reload"

    The broker reads its configuration at startup and does not watch for
    Secret changes. After registering a new server, you must restart the
    broker for it to discover the new tools.

Restart the broker deployment:

```bash
oc rollout restart deployment/mcp-gateway -n mcp-system
```

> **Restart cascade:** When the broker restarts, any agent that was
> already connected to it loses its MCP session. If you have an agent
> deployed, restart it too:
>
> ```bash
> oc rollout restart deployment/workshop-setup-mcp -n workshop-setup-mcp
> ```

## Step 4: Verify Tool Registration

After the broker restarts, test that tools are visible through the gateway.

> **Note:** The MCP streamable-http protocol requires an `initialize`
> call before `tools/list`. For a quick verification, use the
> `initialize` method instead:

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
oc exec -n mcp-system deploy/mcp-gateway -- \
  curl -s http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp \
  -H "Host: openshift.mcp.${CLUSTER_DOMAIN}" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}' \
  | python3 -m json.tool
```

The gateway broker responds with plain JSON (not SSE format), so no
`data:` prefix stripping is needed here unlike the direct server test in
Module 8. This runs curl from inside the cluster against the Istio gateway service,
with a `Host` header matching the HTTPRoute. The response should include
`serverInfo` from the "Kuadrant MCP Gateway" confirming the broker is
serving registered tools.

For a full tool listing, the expected set is 14 tools, all prefixed with `openshift_`:

| Tool | Description |
|---|---|
| openshift_configuration_view | View cluster configuration |
| openshift_events_list | List cluster events |
| openshift_namespaces_list | List namespaces |
| openshift_nodes_log | Get node logs |
| openshift_nodes_stats_summary | Node resource statistics |
| openshift_nodes_top | Node CPU/memory usage |
| openshift_pods_get | Get a specific pod |
| openshift_pods_list | List pods (all namespaces) |
| openshift_pods_list_in_namespace | List pods in a namespace |
| openshift_pods_log | Get pod logs |
| openshift_pods_top | Pod CPU/memory usage |
| openshift_projects_list | List projects |
| openshift_resources_get | Get any resource by GVK |
| openshift_resources_list | List resources by GVK |

If you see 0 tools, the broker may not have restarted. Repeat the rollout
restart in Step 3.

## Step 5: Create MCPVirtualServer Resources

VirtualMCPServers let you expose curated subsets of tools from a single
backend. This is the foundation for role-based tool access -- instead of
deploying multiple servers, you create multiple views of one server's tools.
Administrators get the full toolset while regular users get a read-only
subset.

Create the admin tools virtual server (all 14 tools):

```bash
oc apply -f virtualserver-admin-tools.yaml
```

Create the user tools virtual server (8 read-only tools):

```bash
oc apply -f virtualserver-user-tools.yaml
```

Verify both are created:

```bash
oc get mcpvirtualservers -n mcp-system
```

These MCPVirtualServers are referenced later in AuthPolicy configurations
(Module 10) to route users to different tool subsets based on their identity.

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| HTTPRoute | mcp-ecosystem | Routes `openshift.mcp.<domain>` to the MCP server |
| ReferenceGrant | mcp-system | Allows cross-namespace Gateway reference |
| MCPServerRegistration | mcp-ecosystem | Registers the server with the broker (prefix: openshift_) |
| MCPVirtualServer (admin-tools) | mcp-system | Full 14-tool set for administrators |
| MCPVirtualServer (user-tools) | mcp-system | 8-tool read-only subset for developers |

---

**Next**: [Module 10 -- Identity and Authentication](../10-identity-auth/README.md)
