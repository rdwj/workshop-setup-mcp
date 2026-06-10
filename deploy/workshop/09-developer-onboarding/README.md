# Module 9: Developer Onboarding — Claude Code Against the Gateway

This is the milestone the whole core path builds toward: a developer points
their MCP client (Claude Code, MCP Inspector, any IDE/TUI) at **one gateway
URL**, authenticates as themselves, and gets *their* tools — with every
backend action attributed to them. No per-server configuration, no shared
credentials.

**Time:** 20--30 minutes

**Prerequisites:**
- Modules 1--8 complete
- Claude Code installed locally (or Node.js for MCP Inspector)

## Variables

```bash
CTX="<your-kube-context>"
CLUSTER_DOMAIN=$(oc get ingress.config cluster --context="$CTX" -o jsonpath='{.spec.domain}')
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" -o jsonpath='{.spec.host}')"
GATEWAY_URL="https://mcp-gateway.${CLUSTER_DOMAIN}/mcp"
# CLIENT_SECRET for the mcp-gateway client, from Module 6 Step 10
```

---

## Step 1: Get a Token as developer-a

```bash
TOKEN_A=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-gateway" -d "client_secret=${CLIENT_SECRET}" \
  -d "grant_type=password" -d "username=developer-a" -d "password=developer-a" \
  -d "scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

`scope=openid groups` is required — without the `groups` claim,
VirtualMCPServer routing fails silently and the tool list comes back empty.

> **Production note:** password grants are for the workshop. Real client
> onboarding uses the OAuth device authorization grant or browser-based
> flows; the gateway's `/.well-known` discovery path is already exempted
> from auth in the client-plane policy to support OAuth metadata discovery.
> Tokens expire (1 hour per Module 6); clients re-acquire and reconnect.

## Step 2: Connect Claude Code

Add the gateway as an MCP server with the bearer token:

```bash
claude mcp add --transport http mcp-gateway "${GATEWAY_URL}" \
  --header "Authorization: Bearer ${TOKEN_A}"
```

Then in a Claude Code session, run `/mcp` to confirm the connection and
list the tools. You should see the **admin** tool set (all `openshift_*`
tools, including `openshift_resources_create_or_update`).

The TLS chain works because the gateway hostname is a single-level
subdomain covered by the cluster's wildcard certificate — this is why
Module 2 insisted on `mcp-gateway.<domain>`, not `mcp-gateway.mcp.<domain>`.

**MCP Inspector alternative** (no Claude Code needed):

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP; URL: ${GATEWAY_URL}
# Add header: Authorization: Bearer <token>
```

## Step 3: See Per-User Tool Filtering

Repeat Steps 1--2 as `developer-b` (password `developer-b`), e.g. in a
second terminal:

```bash
claude mcp add --transport http mcp-gateway-b "${GATEWAY_URL}" \
  --header "Authorization: Bearer ${TOKEN_B}"
```

developer-b sees only the read-only subset — the broker filtered
`tools/list` using the wristband (allowed-tools from their client roles)
intersected with their VirtualMCPServer (`user-tools`).

## Step 4: Per-User Enforcement at Call Time

Filtering is cosmetic; enforcement is not. Call a tool outside
developer-b's roles directly (simulating a malicious or misconfigured
client that skips discovery):

```bash
SID=$(curl -sk -D - -o /dev/null -X POST "${GATEWAY_URL}" \
  -H "Authorization: Bearer ${TOKEN_B}" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0.1"}},"id":1}' \
  | grep -i mcp-session-id | tr -d '\r' | awk '{print $2}')

curl -sk -o /dev/null -w "HTTP %{http_code}\n" -X POST "${GATEWAY_URL}" \
  -H "Authorization: Bearer ${TOKEN_B}" -H "Mcp-Session-Id: ${SID}" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"openshift_resources_create_or_update","arguments":{}},"id":2}'
# Expected: HTTP 403 — "Insufficient tool permissions"
```

The 403 comes from the per-route AuthPolicy's Rego checking developer-b's
`resource_access` claim. Tool access is enforced at call time, per user.

## Step 5: Per-User K8s Identity (the payoff)

In Claude Code as **developer-a**, ask:

> Create a ConfigMap named onboarding-demo in the mcp-ecosystem namespace
> with key owner=developer-a

The model calls `openshift_resources_create_or_update`; the gateway passes
developer-a's JWT through; the MCP server uses it for the K8s API call;
K8s RBAC (cluster-admin via mcp-admins) allows it.

As **developer-b**, ask the same thing. Two layers refuse, both per-user:

- If the tool isn't in developer-b's roles: 403 at the gateway (Step 4).
- Even if a Keycloak admin granted developer-b the tool role, the K8s API
  rejects the write — `view` RBAC, *their own identity*, not a shared SA.

Verify attribution in the audit log:

```bash
oc adm node-logs --role=master --path=kube-apiserver/audit.log --context="$CTX" \
  | grep '"user":{"username":"developer-a"' | tail -3
```

The username on the API call is the developer, not `mcp-viewer`. This is
the per-user audit trail that the shared-SA design could not provide.

## Step 6: Onboarding Another Developer (the operational story)

Everything a new developer needs:

1. A Keycloak user in the right group (`mcp-admins` or `mcp-users`)
2. Tool client roles on the bearer-only server clients (Module 6 primer)
3. K8s RBAC via their group (already mapped in Module 7)
4. The one gateway URL and a token

No gateway changes, no policy edits, no per-server credentials. Revocation
is equally central: remove the Keycloak roles/user, and both discovery and
call-time enforcement react on their next token.

---

## What You Demonstrated

| Layer | Mechanism | Scope |
|---|---|---|
| Tool visibility | Wristband + VirtualMCPServer | per user (filtering) |
| Tool invocation | OPA Rego on `resource_access` | per user (enforcement, 403) |
| K8s API authorization | External OIDC + K8s RBAC | per user (the user's own identity) |
| Audit | kube-apiserver audit log | per user attribution |

---

**Next**: [Module 10 -- GitHub MCP Server](../10-github-mcp-server/README.md)
