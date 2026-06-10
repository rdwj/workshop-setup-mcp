# Prerequisites

Verify your workstation tools and cluster access before starting the
workshop. This takes 5 minutes and prevents delays in later modules.

---

## Workstation Tools

| Tool | Used For | Modules | Verify | Install |
|------|----------|---------|--------|---------|
| `oc` | OpenShift CLI | All | `oc version` | [Red Hat OpenShift Downloads](https://console.redhat.com/openshift/downloads) |
| `helm` | Kubernetes package manager | 8--9 | `helm version` | [Helm Install Guide](https://helm.sh/docs/intro/install/) |
| `openssl` | TLS key generation | 7 | `openssl version` | Pre-installed on macOS and Linux |
| `python3` | JSON parsing in test commands | 1, 5--9 | `python3 --version` | [python.org](https://www.python.org/downloads/) |

Run each verify command now. If any command is not found, install it
before proceeding.

!!! note "macOS ships LibreSSL"

    macOS includes LibreSSL rather than OpenSSL. This works fine for the
    key generation in Module 7 -- no action needed.

!!! note "`jq` as an alternative to `python3`"

    The workshop uses `python3` for inline JSON parsing. If you prefer
    `jq`, it works as a drop-in for most commands, but the examples are
    written for `python3`.

---

## Cluster Access

You need `oc` authenticated as **cluster-admin** on an OpenShift 4.16+
cluster. Module 0 installs RHOAI and dependencies if your cluster does
not have them yet. Verify:

```bash
oc whoami
# Expected: kube:admin (or your cluster-admin user)

oc version
# Look for: Server Version: 4.16+ (4.17, 4.18, etc.)
```

If you are not authenticated, log in:

```bash
oc login --server=https://<api-server>:6443 -u <user>
```

---

## Clone the Workshop Repository

Every module references files from this repository. Clone it and work
from the repo root:

```bash
git clone https://github.com/rdwj/workshop-setup-mcp.git
cd workshop-setup-mcp
```

All paths in the workshop (e.g., `deploy/base/`, `deploy/workshop/06-identity-keycloak/`)
are relative to this root.

---

## Set Your Context Variable

The workshop uses a `$CTX` variable to pass `--context` to `oc`
commands. This lets you work with multiple clusters without switching
the active context. Set it once at the start of each terminal session:

```bash
export CTX="<your-kube-context>"
```

To find your context name:

```bash
oc config get-contexts
```

Use the name from the `CURRENT` column (marked with `*`), or the
context pointing to your workshop cluster.

---

## Model Endpoint

You need an OpenAI-compatible model endpoint that supports tool calling.
Module 1 covers this in detail -- you can use a remote API (such as
OpenAI, a hosted vLLM instance, or any compatible provider) or deploy a
local model if your cluster has GPU nodes. Have this ready before
starting Module 8.

---

**Next**: [Module 0 -- Cluster Prerequisites](../00-cluster-prerequisites/README.md)
