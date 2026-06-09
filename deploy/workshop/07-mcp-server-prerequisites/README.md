# Module 7: Prepare MCP Server Prerequisites

This module creates the Kubernetes resources that MCP servers need before they can be deployed. You will create a ServiceAccount, ClusterRoleBinding, and ConfigMap for the OpenShift MCP server.

**Prerequisites** -- Module 6 completed. The MCP Gateway namespace exists.

> **Working directory:**
>
> ```bash
> cd deploy/workshop/07-mcp-server-prerequisites
> ```

---

## Why This Is a Separate Step

The MCP Lifecycle Operator intentionally does not create security-sensitive resources like ServiceAccounts and ClusterRoleBindings. This is a deliberate design decision to prevent privilege escalation -- the operator runs with limited permissions so that a compromised or misconfigured catalog entry cannot create arbitrary cluster-level access. Instead, the MCP Catalog declares what prerequisites each server needs (in its `prerequisites` metadata), and the platform engineer provisions them before deployment. This separation ensures that cluster RBAC changes go through the same review and approval process as any other infrastructure change.

For a deeper look at how the authorization stack is structured, see [Layered Authorization Model](../mcp-layered-authorization.md).

---

## Step 1: Create the mcp-ecosystem Namespace

Create a namespace for MCP server workloads:

```bash
oc create namespace mcp-ecosystem
```

---

## Step 2: Review the Prerequisites

The prerequisites YAML defines three resources:

- A **ServiceAccount** (`mcp-viewer`) that gives the MCP server a Kubernetes API identity
- A **ClusterRoleBinding** granting the built-in `view` ClusterRole to that ServiceAccount (read-only cluster access)
- A **ConfigMap** (`openshift-mcp-server-config`) with the server's runtime configuration

Apply the prerequisites:

```bash
oc apply -f openshift-mcp-prerequisites.yaml
```

---

## Step 3: Verify the Resources

Check that each resource was created:

```bash
oc get sa mcp-viewer -n mcp-ecosystem
oc get clusterrolebinding mcp-viewer-binding
oc get configmap openshift-mcp-server-config -n mcp-ecosystem -o yaml
```

Review the ConfigMap settings. Key configuration values:

- `read_only = true` -- The server will reject any tool calls that would modify cluster state
- `disable_destructive = true` -- Additional safety against destructive operations
- `toolsets = ["core", "config"]` -- Only core cluster inspection and configuration tools are enabled
- Secrets are denied -- The server cannot read Secret resources

---

## What You Created

| Resource | Namespace | Purpose |
|---|---|---|
| ServiceAccount (mcp-viewer) | mcp-ecosystem | K8s API identity for the MCP server |
| ClusterRoleBinding | cluster-scoped | Grants read-only `view` role to mcp-viewer SA |
| ConfigMap | mcp-ecosystem | Server config: read-only mode, denied resources, toolsets |

---

**Next**: [Module 8 -- Deploy the MCP Server](../08-mcp-server/README.md)

