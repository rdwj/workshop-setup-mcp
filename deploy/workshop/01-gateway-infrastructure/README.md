# Module 1: Gateway Infrastructure

This module installs the foundational Gateway API infrastructure that the MCP
Gateway depends on. By the end you will have a working GatewayClass and a
Kuadrant control plane in your cluster.

**Prerequisites** -- RHOAI 3.4 installed with Authorino, cert-manager, and
Service Mesh 3 already present on the cluster.

> **Working directory:** All commands in this module reference files in the
> module directory. Start by changing into it:
>
> ```bash
> cd deploy/workshop/01-gateway-infrastructure
> ```
>
> If you are working with multiple clusters, set your context once and
> append `--context="$CTX"` to each `oc` command:
>
> ```bash
> export CTX="<your-kube-context>"
> ```

---

## Step 1: Install the RHCL Operator

The Red Hat Connectivity Link (RHCL) operator provides the GatewayClass that
both the MaaS and MCP gateways use. Install it as a Subscription in
`openshift-operators`:

```bash
oc apply -f rhcl-subscription.yaml
```

Wait for the CSV to reach `Succeeded`. This can take 2--3 minutes (the
stable channel may install a 1.3.x CSV and immediately upgrade to 1.4.x —
both are fine):

```bash
oc get csv -n openshift-operators | grep rhcl
```

!!! note "On RHCL versions"

    One external MaaS reference configuration reports RHCL 1.4.0 breaking
    RHOAI 3.4 integration. This workshop has been validated end to end on
    both 1.3.4 and 1.4.0 and has never reproduced that breakage. We
    previously tried pinning to 1.3.4 (Manual approval + `startingCSV`),
    but OLM bundles new operator installs in `openshift-operators` into
    the same InstallPlan as the parked RHCL upgrade — which blocks the
    MCP Gateway operator install in Module 2. The pin is therefore not
    viable in a shared namespace; if you ever need it, install the MCP
    Gateway operator in its own namespace first.

!!! warning "InstallPlan May Require Approval"

    On some clusters, OLM bundles the install plan with dependencies from
    other operators and sets it to Manual approval -- even when the
    subscription specifies Automatic. If the CSV doesn't appear after
    2--3 minutes, check for pending InstallPlans:

    ```bash
    oc get installplan -n openshift-operators
    ```

    OLM may bundle the RHCL install into the same InstallPlan as a pending Service Mesh upgrade — approving every unapproved plan (below) handles both. The fast signal is `oc get subscription rhcl-operator -n openshift-operators -o jsonpath='{.status.state}'` showing `UpgradePending`. Approving one plan can immediately surface another pending one — re-run the loop until none remain. If you see any with `APPROVED=false`, approve them:

    ```bash
    for plan in $(oc get installplan -n openshift-operators -o jsonpath='{.items[?(@.spec.approved==false)].metadata.name}'); do
      oc patch installplan "$plan" -n openshift-operators --type=merge -p '{"spec":{"approved":true}}'
    done
    ```

## Step 2: Create the Kuadrant Namespace

Kuadrant needs its own namespace:

```bash
oc apply -f kuadrant-namespace.yaml
```

## Step 3: Create the Kuadrant CR

The Kuadrant custom resource activates the Kuadrant control plane. It deploys
Authorino integration, Limitador, and the policy controllers:

```bash
oc apply -f kuadrant-cr.yaml
```

## Step 4: Wait for Kuadrant to Become Ready

Check the Kuadrant status:

```bash
oc get kuadrant kuadrant -n kuadrant-system -o jsonpath='{.status.conditions}' | python3 -m json.tool
```

Look for the `Ready` condition with `status: "True"`.

!!! important "Kuadrant MissingDependency Race Condition"

    You may see the Kuadrant CR stuck with a `MissingDependency`
    condition. This is less likely if you completed Module 0 (cluster
    prerequisites) before this module, but can still happen if the RHCL
    CRDs are slow to register in the API server. Restarting the operator
    forces a fresh reconciliation loop.

Restart the Kuadrant operator pod to force a re-check:

```bash
oc delete pod -n openshift-operators -l app=kuadrant,control-plane=controller-manager
```

Wait for the new pod to come up, then re-check the Kuadrant status:

```bash
oc get pod -n openshift-operators -l app=kuadrant,control-plane=controller-manager
oc get kuadrant kuadrant -n kuadrant-system -o jsonpath='{.status.conditions}' | python3 -m json.tool
```

The `Ready` condition should now show `"True"`.

## Step 5: Verify a GatewayClass Exists

The RHCL operator (via Service Mesh 3) creates a GatewayClass. Verify it:

```bash
oc get gatewayclasses
```

You should see at least one GatewayClass, typically named
`data-science-gateway-class`. Record this name -- you will need it in Module 2.

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| RHCL Operator Subscription | openshift-operators | Provides GatewayClass via Service Mesh 3 |
| kuadrant-system Namespace | -- | Home for the Kuadrant control plane |
| Kuadrant CR | kuadrant-system | Activates Kuadrant (AuthPolicy, RateLimitPolicy, etc.) |

---

**Next**: [Module 2 -- MCP Gateway](../02-mcp-gateway/README.md)
