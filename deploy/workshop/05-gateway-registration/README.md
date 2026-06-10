# Module 5: Gateway Registration

This module registers the OpenShift MCP server with the MCP Gateway so that
its tools are accessible through a single gateway endpoint. You will also
create VirtualMCPServer resources that expose curated tool subsets for
different user roles.

**Prerequisites** -- Modules 2--4 completed. The OpenShift MCP server pod is
running in `mcp-ecosystem`. The MCP Gateway and broker are running in
`mcp-system`.

> **Working directory:**
>
> ```bash
> cd deploy/workshop/05-gateway-registration
> ```

---

## Step 1: Create the HTTPRoute

The HTTPRoute tells the Gateway how to reach the MCP server. It attaches
to the **backend (`mcps`) listener** with an internal hostname:

```bash
oc apply -f httproute.yaml
```

The hostname is `openshift-mcp-server.mcp.local`, routed to the
`openshift-mcp-server` service on port 8080.

!!! note "Why an internal hostname?"

    Only the broker's in-mesh hairpin ever reaches backend routes — it sets
    the Host header itself, so the hostname doesn't need to be resolvable
    anywhere. Keeping backend routes off the public apps domain means
    clients *cannot* connect to a backend server's hostname directly:
    everyone enters through `https://mcp-gateway.<CLUSTER_DOMAIN>/mcp`
    (Module 2), where the client-plane AuthPolicy lives. This separation is
    what lets per-server policies (Module 8) treat the hairpin leg
    differently per backend.

## Step 2: Create the ReferenceGrant

The HTTPRoute lives in `mcp-ecosystem` but references the Gateway in
`mcp-system`. Kubernetes Gateway API requires an explicit ReferenceGrant in
the Gateway's namespace to allow this cross-namespace reference:

```bash
oc apply -f referencegrant.yaml
```

## Step 3: Create the MCPServerRegistration

The MCPServerRegistration tells the MCP broker about the backend server and
assigns a `toolPrefix`. All tools from this server will be prefixed with
`openshift_` (e.g., `pods_list` becomes `openshift_pods_list`):

```bash
oc apply -f mcpserverregistration.yaml
```

!!! important "`toolPrefix` is Immutable"

    The `toolPrefix` field cannot be changed after the MCPServerRegistration
    is created — it affects tool routing in the broker's configuration cache.
    If you need a different prefix, delete and recreate the resource.
    The CRD field is `toolPrefix` (not `prefix` — using the wrong field
    name is silently ignored and tools appear unprefixed). Plan your naming
    convention before applying.

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
oc exec -n mcp-system deploy/mcp-gateway -- \
  curl -s http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp \
  -H "Host: openshift-mcp-server.mcp.local" \
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

For a full tool listing, the expected set is 15 tools, all prefixed with
`openshift_` (the count depends on config.toml — with `read_only = false`
and `disable_destructive = true` the write tool `resources_create_or_update`
appears; verify against your actual `tools/list` output):

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
| openshift_resources_create_or_update | Create or update a resource (write) |

If you see 0 tools, the broker may not have restarted. Repeat the rollout
restart in Step 3.

## Step 5: Create MCPVirtualServer Resources

VirtualMCPServers let you expose curated subsets of tools from a single
backend. This is the foundation for role-based tool access -- instead of
deploying multiple servers, you create multiple views of one server's tools.
Administrators get the full toolset while regular users get a read-only
subset.

Create the admin tools virtual server (the full tool set, including the write tool):

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

These MCPVirtualServers are referenced later by the client-plane AuthPolicy
(Module 8) to route users to different tool subsets based on their identity.
Creating them now — before the policy that references them — matters: if the
policy's `x-mcp-virtualserver` header points at a VirtualMCPServer that
doesn't exist, users silently see an empty tool list.

## Step 6: Configure Rate Limiting (Optional)

Kuadrant RateLimitPolicy CRDs let you enforce per-user request quotas at
two levels: a gateway-wide default and per-route overrides. Both use
`auth.identity.preferred_username` from the Keycloak JWT as the counter
key, so limits track individual users rather than source IPs.

> **Note:** Rate limiting depends on JWT claims from Keycloak. The policies
> can be applied now, but enforcement only takes effect after Modules 6--8
> set up identity and authentication.

### Gateway-level default

This policy attaches to the Gateway's **client (`mcp`) listener** — it
limits users where they enter. (Targeting `mcps` would throttle the
broker's own hairpin traffic to backends.) It allows 10 requests per minute
per user:

```bash
oc apply -f ratelimitpolicy-gateway.yaml
```

### Per-server override

This policy attaches directly to the OpenShift MCP server's HTTPRoute and
overrides the gateway default. It allows 5 requests per minute per user
for this specific server:

```bash
oc apply -f ratelimitpolicy-per-server.yaml
```

The two-tier model works because Kuadrant's `defaults:` block (used in the
gateway policy) is inherited by routes unless the route has its own
`limits:` block. Routes with an explicit policy always win.

### Verify rate limiting

After completing Module 8, you can confirm rate limiting is active by
sending requests that exceed the limit. The gateway returns HTTP 429 when
the quota is exhausted:

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
TOKEN=$(cat /tmp/mcp-token)  # Acquired in Module 6

for i in $(seq 1 6); do
  curl -s -o /dev/null -w "Request ${i}: HTTP %{http_code}\n" \
    "https://mcp-gateway.${CLUSTER_DOMAIN}/mcp" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}'
done
```

The first 5 requests should return HTTP 200; the 6th should return HTTP 429.

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| HTTPRoute | mcp-ecosystem | Routes `openshift-mcp-server.mcp.local` (backend plane) to the MCP server |
| ReferenceGrant | mcp-system | Allows cross-namespace Gateway reference |
| MCPServerRegistration | mcp-ecosystem | Registers the server with the broker (prefix: openshift_) |
| MCPVirtualServer (admin-tools) | mcp-system | Full tool set (incl. write tool) for administrators |
| MCPVirtualServer (user-tools) | mcp-system | 8-tool read-only subset for developers |
| RateLimitPolicy (mcp-gateway-ratelimit) | mcp-system | 10 req/min per user on the client listener (default) |
| RateLimitPolicy (openshift-mcp-ratelimit) | mcp-ecosystem | 5 req/min per user for the OpenShift MCP server (override) |

---

**Next**: [Module 6 -- Identity: Keycloak](../06-identity-keycloak/README.md)
