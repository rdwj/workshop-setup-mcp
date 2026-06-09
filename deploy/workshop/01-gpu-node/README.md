# Module 1: Add a GPU Compute Node

Provision a GPU worker node on AWS and install the NVIDIA GPU Operator
for on-cluster model serving. The module creates the MachineSet first
so AWS starts provisioning immediately, then installs the GPU Operator
and NFD while the node provisions.

**Time:** 15--20 minutes (plus 10--15 minutes for node provisioning)

!!! note "Skippable -- no GPU required for the core workshop"
    If your cluster does not have GPU nodes or you plan to use an
    external model endpoint, skip to
    [Module 2 -- Gateway Infrastructure](../02-gateway-infrastructure/README.md).
    You can return to this module later if needed.

**Prerequisites:**

- Module 0 completed
- AWS-based OpenShift cluster with access to GPU instance types
- Cluster administrator privileges

> **Working directory:** `cd deploy/workshop/01-gpu-node`

## Variables

```bash
export CTX="<your-kube-context>"
```

## Step 1: Discover MachineSet Parameters

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

## Step 2: Create the GPU MachineSet

Create the MachineSet first so AWS starts provisioning the instance
immediately. The GPU Operator installs in the next step while the node
provisions.

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

## Step 3: Install the GPU Operator and NFD

While the GPU node provisions, install the NVIDIA GPU Operator and
Node Feature Discovery:

```bash
oc apply -f nfd-operator.yaml --context="$CTX"
oc apply -f gpu-operator.yaml --context="$CTX"
```

Wait for both operators to install:

```bash
echo "Waiting for NFD operator..."
until oc get csv -n openshift-nfd --context="$CTX" 2>/dev/null | grep -q Succeeded; do
  sleep 10
done
echo "NFD operator ready"

echo "Waiting for GPU operator..."
until oc get csv -n nvidia-gpu-operator --context="$CTX" 2>/dev/null | grep -q Succeeded; do
  sleep 10
done
echo "GPU operator ready"
```

This typically takes 2--3 minutes.

## Step 4: Create NFD Instance and ClusterPolicy

Create the Node Feature Discovery instance so the cluster can detect
GPU hardware on nodes:

```bash
oc apply -f nfd-instance.yaml --context="$CTX"
```

Create the ClusterPolicy, which tells the GPU Operator how to configure
NVIDIA drivers, the device plugin, and monitoring:

```bash
oc apply -f gpu-cluster-policy.yaml --context="$CTX"
```

!!! note "ClusterPolicy status"
    The ClusterPolicy will show `notReady` until a GPU node joins the
    cluster. This is normal -- the drivers install automatically when
    the node appears.

## Step 5: Wait for the GPU Node

Check whether the GPU node has joined the cluster. If you continued
from Step 2 without waiting, the node may already be ready:

```bash
oc get machines -n openshift-machine-api --context="$CTX" | grep gpu
```

If the phase is not yet `Running`, watch until it is:

```bash
oc get machines -n openshift-machine-api --context="$CTX" -w | grep gpu
```

Then verify the node joined:

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
# GPU Operator installed
oc get csv -n nvidia-gpu-operator --context="$CTX" | grep gpu-operator
# Expected: Succeeded

# NFD installed
oc get csv -n openshift-nfd --context="$CTX" | grep nfd
# Expected: Succeeded

# ClusterPolicy ready
oc get clusterpolicy gpu-cluster-policy --context="$CTX" \
  -o jsonpath='{.status.state}'
# Expected: ready

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
| NFD Operator | `openshift-nfd` | Node Feature Discovery for GPU detection |
| GPU Operator | `nvidia-gpu-operator` | NVIDIA GPU support (drivers, device plugin) |
| NodeFeatureDiscovery | `openshift-nfd` | NFD instance for hardware feature labels |
| ClusterPolicy `gpu-cluster-policy` | (cluster-wide) | Configures NVIDIA drivers, device plugin, and monitoring on GPU nodes |
| MachineSet `<infra-id>-gpu-<az>` | `openshift-machine-api` | Provisions a `g6e.4xlarge` GPU instance on AWS |
| GPU Node | (cluster) | Worker node with NVIDIA GPU and `nvidia.com/gpu:NoSchedule` taint |
| HardwareProfile `nvidia-gpu` | `redhat-ods-applications` | RHOAI scheduling profile with GPU resource defaults and taint tolerations |

---

**Next**: [Module 2 -- Gateway Infrastructure](../02-gateway-infrastructure/README.md)

!!! note "Continue while the GPU node provisions"
    If the GPU node is still provisioning or drivers are still
    installing, continue with Module 2. You will verify the GPU node
    in Module 3 before deploying a model.
