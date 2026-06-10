# Module 8+: Deploy the GitHub MCP Server

This module deploys the official GitHub MCP server as a second backend behind
the MCP Gateway. Unlike the OpenShift MCP server (which uses the MCPServer CR
and lifecycle operator), this is a third-party container deployed as a standard
Deployment + Service. It runs in `--read-only` mode, exposing 25 GitHub tools
with a `github_` prefix.

**Prerequisites** -- Module 8 completed. The `mcp-ecosystem` namespace exists.
Module 9 (Gateway Registration) completed -- the ReferenceGrant allowing
cross-namespace HTTPRoute references from `mcp-ecosystem` to `mcp-system` is
already in place.

> **Working directory:**
>
> ```bash
> cd deploy/workshop/08-github-mcp-server
> ```

---

## Step 1: Create a GitHub Personal Access Token

You need a GitHub PAT for the server to authenticate with the GitHub API.

Create a **fine-grained** token at <https://github.com/settings/personal-access-tokens/new>
with these repository permissions on whichever repos you want the agent to access:

- **Contents**: Read-only
- **Metadata**: Read-only

Alternatively, a **classic** token with `repo` scope works but grants broader
access than necessary.

Copy the token value -- you will need it in the next step.

## Step 2: Create the Secret

The Secret stores your PAT and makes it available to both the MCP server pod
and the gateway's credential injection.

Edit `github-mcp-secret.yaml` and replace `<GITHUB_PAT>` with your token, then
apply:

```bash
oc apply -f github-mcp-secret.yaml
```

!!! important "The `mcp.kuadrant.io/secret: \"true\"` Label is Required"

    The Secret **must** have the label `mcp.kuadrant.io/secret: "true"`.
    Without it, the MCP Gateway controller silently fails to reconcile the
    MCPServerRegistration -- the error only appears in the controller pod
    logs in the `openshift-operators` namespace, not in the registration
    status. The provided manifest already includes this label.

## Step 3: Deploy the Server

Apply the Deployment and Service:

```bash
oc apply -f github-mcp-server.yaml
```

Wait for the pod to start:

```bash
oc get pods -n mcp-ecosystem -l app=github-mcp-server -w
```

You should see `github-mcp-server-*` with status `Running`. The server
listens on port 8082.

## Step 4: Create the HTTPRoute

The HTTPRoute maps a hostname to the GitHub MCP server through the gateway.
Substitute your cluster domain and apply:

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
sed "s/<CLUSTER_DOMAIN>/${CLUSTER_DOMAIN}/g" github-httproute.yaml | oc apply -f -
```

The hostname will be `mcp-github.<CLUSTER_DOMAIN>`, routed to the
`github-mcp-server` service on port 8082.

> **Note:** The ReferenceGrant created in Module 9 already permits
> cross-namespace references from `mcp-ecosystem` to the Gateway in
> `mcp-system`. No additional ReferenceGrant is needed.

## Step 5: Register with the Gateway

The MCPServerRegistration tells the broker about the GitHub MCP server and
assigns the `github_` tool prefix:

```bash
oc apply -f github-mcpserverregistration.yaml
```

!!! important "`toolPrefix` is Immutable"

    The `toolPrefix` field cannot be changed after the MCPServerRegistration
    is created. If you need a different prefix, delete and recreate the
    resource. The CRD field is `toolPrefix` (not `prefix` -- using the wrong
    field name is silently ignored and tools appear unprefixed).

!!! important "Broker Does Not Auto-Reload"

    After registering a new server, you must restart the broker for it to
    discover the new tools.

Restart the broker:

```bash
oc rollout restart deployment/mcp-gateway -n mcp-system
```

Wait for the rollout to complete:

```bash
oc rollout status deployment/mcp-gateway -n mcp-system
```

> **Restart cascade:** If you already have an agent deployed, restart it too
> -- it will have lost its MCP session when the broker restarted:
>
> ```bash
> oc rollout restart deployment/workshop-setup-mcp -n workshop-setup-mcp
> ```

## Step 6: Verify Tool Registration

Test that the GitHub tools are visible through the gateway:

```bash
CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
oc exec -n mcp-system deploy/mcp-gateway -- \
  curl -s http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp \
  -H "Host: mcp-github.${CLUSTER_DOMAIN}" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}' \
  | python3 -m json.tool
```

The response should include `serverInfo` from the "Kuadrant MCP Gateway"
confirming the broker is serving the registered GitHub tools. If you see 0
tools, the broker may not have finished restarting -- repeat the rollout
restart in Step 5.

The expected set is 25 read-only tools, all prefixed with `github_`:

| Tool | Description |
|---|---|
| github_get_commit | Get details for a commit |
| github_get_file_contents | Get file or directory contents |
| github_get_label | Get a specific label from a repository |
| github_get_latest_release | Get the latest release |
| github_get_me | Get the authenticated user |
| github_get_release_by_tag | Get a release by tag name |
| github_get_tag | Get details about a git tag |
| github_get_team_members | Get team members |
| github_get_teams | Get the user's teams |
| github_issue_read | Read issue details, comments, sub-issues, or labels |
| github_list_branches | List branches |
| github_list_commits | List commits on a branch |
| github_list_issue_types | List issue types for an organization |
| github_list_issues | List issues in a repository |
| github_list_pull_requests | List pull requests |
| github_list_releases | List releases |
| github_list_repository_collaborators | List repository collaborators |
| github_list_tags | List git tags |
| github_pull_request_read | Read PR details, diff, status, files, reviews, comments, or check runs |
| github_search_code | Search code across repositories |
| github_search_commits | Search commits |
| github_search_issues | Search issues |
| github_search_pull_requests | Search pull requests |
| github_search_repositories | Search repositories |
| github_search_users | Search users |

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| Secret | mcp-ecosystem | GitHub PAT for API authentication |
| Deployment + Service | mcp-ecosystem | GitHub MCP server (read-only, port 8082) |
| HTTPRoute | mcp-ecosystem | Routes `mcp-github.<domain>` to the server |
| MCPServerRegistration | mcp-ecosystem | Registers the server with the broker (prefix: github_) |

---

## Limitations

### Single-Identity Model

The official GitHub MCP server is designed for a single developer on their
local machine. It authenticates all GitHub API requests with the PAT
configured in the `github-mcp-token` Secret — there is no per-request
identity pass-through.

When accessed directly, each developer can use their own GitHub credentials.
When accessed through the MCP Gateway, all requests use the shared PAT
regardless of which user's JWT authorized the gateway request. This means:

- The server is deployed in `--read-only` mode to prevent unattributed writes
- All GitHub API rate limits are shared across users of the gateway
- Audit logs on GitHub show the PAT owner, not the individual developer

Supporting per-user GitHub identity through the gateway would require a custom
MCP server that accepts pass-through identity headers and maps them to
per-user credentials (for example, via HashiCorp Vault dynamic secrets from
Module 14).

---

**Next**: [Module 10 -- Identity and Authentication](../10-identity-auth/README.md)
