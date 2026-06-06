# Module 2: Gateway Infrastructure

This module installs the foundational Gateway API infrastructure that the MCP
Gateway depends on. By the end you will have a working GatewayClass and a
Kuadrant control plane in your cluster.

**Prerequisites** -- RHOAI 3.4 installed with Authorino, cert-manager, and
Service Mesh 3 already present on the cluster.

> **Working directory:** All commands in this module reference files in the
> module directory. Start by changing into it:
>
> ```bash
> cd deploy/workshop/02-gateway-infrastructure
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

The subscription uses `installPlanApproval: Automatic`, so OLM will install the
operator without manual approval. Wait for the CSV to reach `Succeeded`:

```bash
oc get csv -n openshift-operators | grep rhcl
```

You should see something like `rhcl-operator.v1.3.4` with phase `Succeeded`.
This can take 2-3 minutes.

> **Note:** If the CSV doesn't appear after 3 minutes, check for pending
> InstallPlans that need approval:
>
> ```bash
> oc get installplan -n openshift-operators
> ```
>
> If you see any with `APPROVED=false`, approve them:
>
> ```bash
> for plan in $(oc get installplan -n openshift-operators -o jsonpath='{.items[?(@.spec.approved==false)].metadata.name}'); do
>   oc patch installplan "$plan" -n openshift-operators --type=merge -p '{"spec":{"approved":true}}'
> done
> ```

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
`data-science-gateway-class`. Record this name -- you will need it in Module 3.

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| RHCL Operator Subscription | openshift-operators | Provides GatewayClass via Service Mesh 3 |
| kuadrant-system Namespace | -- | Home for the Kuadrant control plane |
| Kuadrant CR | kuadrant-system | Activates Kuadrant (AuthPolicy, RateLimitPolicy, etc.) |
