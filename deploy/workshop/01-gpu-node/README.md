# Module 1: Add a GPU Compute Node

This module provisions a dedicated GPU worker node on AWS and configures OpenShift AI to schedule GPU workloads. You will create a MachineSet for NVIDIA GPU instances, verify the node joins the cluster, and create a HardwareProfile for RHOAI workbenches.

**Time:** 15--20 minutes (plus 10--15 minutes for node provisioning)

!!! note "Skippable -- no GPU required for the core workshop"
    If your cluster does not have GPU nodes or you plan to use an
    external model endpoint, skip to
    [Module 2 -- Gateway Infrastructure](../02-gateway-infrastructure/README.md).
    You can return to this module later if needed.

**Prerequisites:**

- Module 0 completed (GPU Operator and NFD installed)
- AWS-based OpenShift cluster with access to GPU instance types
- Cluster administrator privileges

> **Working directory:** `cd deploy/workshop/01-gpu-node`

## Variables

```bash
export CTX="<your-kube-context>"
```

## Step 1: Verify GPU Infrastructure

Check that the GPU Operator and NFD are running (installed in Module 0):

```bash
oc get csv -n nvidia-gpu-operator --context="$CTX" | grep gpu-operator
oc get csv -n openshift-nfd --context="$CTX" | grep nfd
```

Both should show `Succeeded`.

!!! note "If the GPU Operator is not installed"
    Module 0 installs the GPU Operator and NFD as part of the base
    deployment. If you skipped Module 0 or the operators are not
    running, return to Module 0 and apply `deploy/base/`.

## Step 2: Create the ClusterPolicy

The ClusterPolicy tells the GPU Operator how to configure NVIDIA drivers,
the device plugin, and monitoring on GPU nodes. Create it if it doesn't
already exist:

```bash
oc get clusterpolicy gpu-cluster-policy --context="$CTX" 2>/dev/null \
  && echo "ClusterPolicy already exists" \
  || oc apply -f gpu-cluster-policy.yaml --context="$CTX"
```

Wait for the ClusterPolicy to reach the `ready` state:

```bash
oc get clusterpolicy gpu-cluster-policy --context="$CTX" \
  -o jsonpath='{.status.state}'
```

Expected: `ready`. If it shows `notReady`, that is normal -- the policy
will finish initializing after a GPU node joins the cluster.

## Step 3: Discover MachineSet Parameters

You need six values from your cluster to create the GPU MachineSet. Extract
them from an existing worker MachineSet:

```bash
# Get the name of an existing worker MachineSet
WORKER_MS=$(oc get machineset -n openshift-machine-api --context="$CTX" \
  -o jsonpath='{.items[0].metadata.name}')
echo "Using MachineSet: $WORKER_MS"
```

Then extract each value:

```bash
# Cluster infrastructure ID
INFRA_ID=$(oc get infrastructure cluster --context="$CTX" \
  -o jsonpath='{.status.infrastructureName}')
echo "INFRA_ID=$INFRA_ID"

# AWS region
REGION=$(oc get machineset "$WORKER_MS" -n openshift-machine-api --context="$CTX" \
  -o jsonpath='{.spec.template.spec.providerSpec.value.placement.region}')
echo "REGION=$REGION"

# Availability zone (pick one from your cluster)
AZ=$(oc get machineset "$WORKER_MS" -n openshift-machine-api --context="$CTX" \
  -o jsonpath='{.spec.template.spec.providerSpec.value.placement.availabilityZone}')
echo "AZ=$AZ"

# RHCOS AMI ID for the region
AMI_ID=$(oc get machineset "$WORKER_MS" -n openshift-machine-api --context="$CTX" \
  -o jsonpath='{.spec.template.spec.providerSpec.value.ami.id}')
echo "AMI_ID=$AMI_ID"

# Subnet filter value
SUBNET=$(oc get machineset "$WORKER_MS" -n openshift-machine-api --context="$CTX" \
  -o jsonpath='{.spec.template.spec.providerSpec.value.subnet.filters[0].values[0]}')
echo "SUBNET=$SUBNET"

# Security group filter value
SECURITY_GROUP=$(oc get machineset "$WORKER_MS" -n openshift-machine-api --context="$CTX" \
  -o jsonpath='{.spec.template.spec.providerSpec.value.securityGroups[0].filters[0].values[0]}')
echo "SECURITY_GROUP=$SECURITY_GROUP"
```

!!! warning "Verify all six values"
    All variables must be non-empty before proceeding. If any value is
    blank, inspect the worker MachineSet with
    `oc get machineset $WORKER_MS -n openshift-machine-api --context="$CTX" -o yaml`
    and locate the field manually.

## Step 4: Create the GPU MachineSet

=== "Approach A: Parameterized Template"

    Substitute the placeholders in the template and apply:

    ```bash
    sed -e "s/<INFRA_ID>/${INFRA_ID}/g" \
        -e "s/<REGION>/${REGION}/g" \
        -e "s/<AZ>/${AZ}/g" \
        -e "s/<AMI_ID>/${AMI_ID}/g" \
        -e "s/<SUBNET>/${SUBNET}/g" \
        -e "s/<SECURITY_GROUP>/${SECURITY_GROUP}/g" \
        gpu-machineset-template.yaml \
      | oc apply --context="$CTX" -f -
    ```

=== "Approach B: Clone an Existing MachineSet"

    Clone a worker MachineSet and patch it for GPU use:

    ```bash
    oc get machineset "$WORKER_MS" -n openshift-machine-api --context="$CTX" -o json \
      | jq '
        .metadata.name = "'${INFRA_ID}'-gpu-'${AZ}'"
        | .metadata.resourceVersion = null
        | .spec.replicas = 1
        | .spec.selector.matchLabels."machine.openshift.io/cluster-api-machineset" = "'${INFRA_ID}'-gpu-'${AZ}'"
        | .spec.template.metadata.labels."machine.openshift.io/cluster-api-machineset" = "'${INFRA_ID}'-gpu-'${AZ}'"
        | .spec.template.spec.metadata.labels."node-role.kubernetes.io/gpu" = ""
        | .spec.template.spec.taints = [{"key": "nvidia.com/gpu", "effect": "NoSchedule"}]
        | .spec.template.spec.providerSpec.value.instanceType = "g6e.4xlarge"
        | .spec.template.spec.providerSpec.value.blockDevices[0].ebs.volumeSize = 200
        | .spec.template.spec.providerSpec.value.blockDevices[0].ebs.volumeType = "gp3"
      ' | oc apply --context="$CTX" -f -
    ```

Verify the MachineSet was created:

```bash
oc get machineset -n openshift-machine-api --context="$CTX" | grep gpu
```

## Step 5: Wait for the GPU Node

The node takes 10--15 minutes to provision. Watch the Machine status:

```bash
oc get machines -n openshift-machine-api --context="$CTX" -w | grep gpu
```

Wait until the phase shows `Running`. Then verify the node joined the cluster:

```bash
oc get nodes --context="$CTX" -l node-role.kubernetes.io/gpu=
```

!!! note "GPU driver installation"
    After the node joins, the GPU Operator installs the NVIDIA driver,
    device plugin, and feature discovery daemon. This takes another
    2--3 minutes. Wait for the `nvidia.com/gpu` resource to appear in
    the node's allocatable resources before proceeding.

## Step 6: Verify GPU Capacity

```bash
oc get node -l node-role.kubernetes.io/gpu= --context="$CTX" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}gpu={.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
```

Expected output: the GPU node name with `gpu=1`.

## Step 7: Create the GPU HardwareProfile

The HardwareProfile tells RHOAI how to schedule GPU workloads, including
resource defaults and GPU taint tolerations:

```bash
oc apply -f nvidia-gpu-hardwareprofile.yaml --context="$CTX"
```

Verify:

```bash
oc get hardwareprofile nvidia-gpu -n redhat-ods-applications --context="$CTX"
```

## Verify

Run all checks:

```bash
# GPU node is ready
oc get nodes --context="$CTX" -l node-role.kubernetes.io/gpu= \
  -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}'
# Expected: True

# GPU is allocatable
oc get node -l node-role.kubernetes.io/gpu= --context="$CTX" \
  -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'
# Expected: 1

# HardwareProfile exists
oc get hardwareprofile nvidia-gpu -n redhat-ods-applications --context="$CTX" \
  -o jsonpath='{.metadata.name}'
# Expected: nvidia-gpu
```

## What You Deployed

| Resource | Namespace | Purpose |
|----------|-----------|---------|
| ClusterPolicy `gpu-cluster-policy` | (cluster-wide) | Configures NVIDIA drivers, device plugin, and monitoring on GPU nodes |
| MachineSet `<infra-id>-gpu-<az>` | `openshift-machine-api` | Provisions a `g6e.4xlarge` GPU instance on AWS |
| GPU Node | (cluster) | Worker node with NVIDIA GPU and `nvidia.com/gpu:NoSchedule` taint |
| HardwareProfile `nvidia-gpu` | `redhat-ods-applications` | RHOAI scheduling profile with GPU resource defaults and taint tolerations |

---

**Next**: [Module 2 -- Gateway Infrastructure](../02-gateway-infrastructure/README.md)

!!! note "Continue while the GPU node provisions"
    The MachineSet takes 10--15 minutes to provision. Continue with
    Module 2 while you wait -- you will verify the GPU node in
    Module 3.
