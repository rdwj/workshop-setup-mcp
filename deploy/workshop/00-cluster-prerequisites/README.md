# Module 0: Cluster Prerequisites

Install the platform operators and operands that the workshop depends on.
By the end of this module your cluster will have RHOAI 3.4, Service Mesh 3,
cert-manager, and the supporting operators ready for the MCP Gateway stack.

**Time:** 15--20 minutes (plus 5--10 minutes for operators to install)

> **Working directory:** all commands in this module run from the **repo
> root** (`cd workshop-setup-mcp` after cloning). If you opened a new
> terminal, change back into the repo first.

## Can I Skip This?

If your cluster already has these prerequisites, skip to
[Module 1 -- Gateway Infrastructure](../01-gateway-infrastructure/README.md). Check with:

```bash
# RHOAI operator installed?
oc get csv -A | grep rhods-operator

# Service Mesh 3 installed? (comes with RHOAI)
oc get csv -A | grep servicemeshoperator3

# GatewayClass exists? (created by Service Mesh 3)
oc get gatewayclasses
```

If all three commands return results, skip this module.

---

## Variables

Set your cluster context once and use it throughout the workshop:

```bash
export CTX="<your-kube-context>"
```

## Step 1: First Pass -- Operators

The `deploy/base/` directory contains a Kustomize overlay that creates
namespaces and operator subscriptions. The first pass will partially fail
on operand CRs because the CRDs don't exist yet -- this is expected:

```bash
oc apply -k deploy/base --context="$CTX"
```

You will see errors like `no matches for kind "DataScienceCluster"` --
these are normal. The operator subscriptions are being processed.

## Step 2: Wait for Operators

Monitor the operator installations until all show `Succeeded`:

```bash
oc get csv -A --context="$CTX" | grep -E 'Succeeded|Installing'
```

Look for these key operators:
- `rhods-operator` (Red Hat OpenShift AI)
- `servicemeshoperator3` (Red Hat OpenShift Service Mesh 3)
- `cert-manager-operator`

!!! note "GPU Operator and NFD"
    The GPU Operator and Node Feature Discovery are installed in
    [Module 12](../12-gpu-node/README.md) in the optional model track, not
    here. GPU/model work is deferred until after the core path.

This typically takes 3--5 minutes. If an operator stays in `Installing`
for more than 5 minutes, check for pending InstallPlans:

```bash
oc get installplan -A --context="$CTX" | grep -v 'true'
```

Approve any pending plans:

```bash
for plan in $(oc get installplan -n openshift-operators --context="$CTX" \
  -o jsonpath='{.items[?(@.spec.approved==false)].metadata.name}'); do
  oc patch installplan "$plan" -n openshift-operators --context="$CTX" \
    --type=merge -p '{"spec":{"approved":true}}'
done
```

> If you re-run this loop *after* Module 1, skip any plan whose CSV list
> contains `rhcl-operator.v1.4` — that parked plan is the RHCL 1.3.x
> version pin working as intended (see Module 1).

## Step 3: Second Pass (and Third) -- Operands

Once all operators show `Succeeded`, run the overlay again. This time the
operand CRs (DataScienceCluster, OdhDashboardConfig) will be
created successfully:

```bash
oc apply -k deploy/base --context="$CTX"
```

If any resource still fails, wait 30 seconds and run the command again.
Two or three passes is normal -- CRDs register asynchronously after
their operator starts, so a single retry is not always enough.
You are done when the command exits cleanly with no errors.

!!! note "OdhDashboardConfig"

    `OdhDashboardConfig` is typically the last CRD to appear because it
    depends on the `redhat-ods-applications` namespace, which RHOAI
    creates during its own initialization. If this resource fails,
    wait ~30 seconds and re-run -- it does not indicate a real problem.

## Step 4: Verify

Confirm the key components are ready:

```bash
# RHOAI operator
oc get csv -n redhat-ods-operator --context="$CTX" | grep rhods

# Service Mesh 3
oc get csv -n openshift-operators --context="$CTX" | grep servicemeshoperator3

# GatewayClass (created by Service Mesh 3)
oc get gatewayclasses --context="$CTX"
```

You should see a GatewayClass named `data-science-gateway-class` with
`ACCEPTED=True`.

---

## What You Installed

| Component | Purpose |
|-----------|---------|
| RHOAI 3.4 | AI platform (brings Service Mesh 3 as a dependency) |
| Service Mesh 3 | Provides Istio and GatewayClass for the MCP Gateway |
| cert-manager | TLS certificate management |
| Web Terminal | In-browser terminal for cluster access |

---

**Next**: [Module 1 -- Gateway Infrastructure](../01-gateway-infrastructure/README.md)
