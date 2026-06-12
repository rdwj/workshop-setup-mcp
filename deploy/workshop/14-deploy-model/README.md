# Module 14: Deploy a Model from the Catalog (Optional Model Track)

Deploy the Red Hat gpt-oss-20b model on your GPU node as an
**LLMInferenceService** — the llm-d serving path that RHOAI 3.x's Model
Catalog uses. This resource type is MaaS-native: its router attaches to
the `maas-default-gateway` from Module 13, so the model is automatically
published through MaaS at
`https://inference.maas.<CLUSTER_DOMAIN>/gpt-oss-model/redhataigpt-oss-20b`
and appears as a Gen AI asset in the dashboard. There is no separate
"add to MaaS" step — it is intrinsic to deploying this way.

**Time:** 15--25 minutes (plus 5--10 minutes for model weight download)

!!! note "Skippable -- only needed for on-cluster model serving"
    If you skipped Modules 12 and 13, skip this module and continue to
    [Module 15 -- Model Endpoint](../15-model-endpoint/README.md).

**Prerequisites:**

- Module 12 complete (GPU node with HardwareProfile)
- Module 13 complete (MaaS infrastructure running)

> **Working directory:** `cd deploy/workshop/14-deploy-model`

## Variables

```bash
export CTX="<your-kube-context>"
```

## Step 1: Deploy the Model

=== "Approach A: RHOAI Dashboard"

    1. Open the RHOAI Dashboard in your browser
    2. Navigate to **Model Catalog** in the left sidebar
    3. Find **gpt-oss-20b** in the catalog and click it
    4. Click **Deploy**
    5. Under **Hardware profile**, select **NVIDIA GPU**
    6. Under **Project**, select or create `gpt-oss-model`
    7. Leave replicas at 1 and click **Deploy**

    The dashboard creates an LLMInferenceService (plus a per-service copy
    of the vLLM CUDA config) for you. Skip to Step 2 to monitor the
    deployment.

=== "Approach B: CLI"

    Apply the deployment manifest, which creates exactly what the
    dashboard catalog deploy creates:

    ```bash
    oc apply -f gpt-oss-20b-deployment.yaml --context="$CTX"
    ```

    The manifest deploys four resources:

    - **Namespace** `gpt-oss-model` with the RHOAI dashboard label
    - **Secret** `gpt-oss-20b-connection` pointing to the OCI model image
      (`oci://registry.redhat.io/rhelai1/modelcar-gpt-oss-20b:1.5`)
    - **LLMInferenceServiceConfig** `redhataigpt-oss-20b` — per-service
      copy of the well-known vLLM NVIDIA CUDA template
    - **LLMInferenceService** `redhataigpt-oss-20b` with 1 GPU, the
      `nvidia-gpu` HardwareProfile, and a router attached to the MaaS
      gateway

!!! warning "One GPU, one model"
    The workshop GPU node has a single GPU. Do not deploy via *both*
    approaches (or deploy the same model twice): the second deployment's
    pod sits `Pending` with `Insufficient nvidia.com/gpu` until the first
    is deleted.

## Step 2: Wait for the Model

The KServe controller schedules the workload pod
(`redhataigpt-oss-20b-kserve-...`) on your GPU node. The pod downloads
model weights from the OCI registry, which takes 5--10 minutes:

```bash
oc get pods -n gpt-oss-model --context="$CTX" -w
```

Wait until the pod shows `2/2 Running` (the model container plus the
llm-d routing sidecar — it sits at `1/2` while vLLM loads weights).

!!! note "Model weight download"
    The gpt-oss-20b model weights are pulled from
    `registry.redhat.io/rhelai1/modelcar-gpt-oss-20b:1.5` as an OCI
    artifact. If the pod stays not-ready for more than 10 minutes, watch
    the model container logs:
    `oc logs -n gpt-oss-model --context="$CTX" -l app.kubernetes.io/name=redhataigpt-oss-20b -c main -f`

## Step 3: Verify the LLMInferenceService

```bash
oc get llminferenceservice redhataigpt-oss-20b -n gpt-oss-model --context="$CTX"
```

The `READY` column should show `True`, and `URL` shows the MaaS-published
endpoint — the model is already routed through the MaaS gateway:

```bash
oc get llminferenceservice redhataigpt-oss-20b -n gpt-oss-model --context="$CTX" \
  -o jsonpath='{.status.url}{"\n"}'
# https://inference.maas.<CLUSTER_DOMAIN>/gpt-oss-model/redhataigpt-oss-20b
```

## Step 4: Test the Model Endpoint

KServe creates an in-cluster Service for the workload —
`redhataigpt-oss-20b-kserve-workload-svc` on port 8000 — so no extra
test Service is needed. **The workload serves TLS** (KServe mounts
self-signed certs at `/var/run/kserve/tls`), so the test must use
`https://` with `-k`; plain `http://` fails silently with curl exit
code 52 and no output, even though the model is healthy. Test from
within the cluster:

```bash
# Note: https + -k (self-signed KServe cert); no -t (tty) and
# grep-based extraction — pod lifecycle messages share the stream and
# break strict JSON parsing
oc run curl-test -n gpt-oss-model --context="$CTX" --rm -i \
  --image=registry.redhat.io/ubi9/ubi-minimal:latest \
  --restart=Never -- \
  curl -sk https://redhataigpt-oss-20b-kserve-workload-svc:8000/v1/models \
  | grep -o '"id":"[^"]*"' 
```

You should see `redhataigpt-oss-20b` in the output.

!!! note "Check the RHOAI Dashboard"
    Open the RHOAI Dashboard: the model appears under the project's
    **Models** with a green status, and (because it carries the
    `genai-asset` label) under **AI hub** as an available asset.

## Try Other Models

The Red Hat Model Catalog includes several models you can deploy using
the same process:

- **granite-3.3-8b-instruct** -- smaller, faster, good for testing
- **granite-3.3-2b-instruct** -- smallest, suitable for limited GPU memory
- Other models from the `registry.redhat.io/rhelai1/` registry

To deploy a different model, use the RHOAI Dashboard Model Catalog
(Approach A above) and select the model you want.

!!! warning "Tool calling support"
    Not all models support tool calling. The gpt-oss-20b model is
    recommended for this workshop because it handles tool calling
    well. If you deploy a different model, verify it supports tool
    calling before using it with the MCP Gateway agent.

## Verify

```bash
# LLMInferenceService is ready
oc get llminferenceservice redhataigpt-oss-20b -n gpt-oss-model --context="$CTX" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
# Expected: True

# Workload pod is running on the GPU node
oc get pods -n gpt-oss-model --context="$CTX" \
  -l app.kubernetes.io/name=redhataigpt-oss-20b \
  -o jsonpath='{.items[0].spec.nodeName}'
# Expected: your GPU node name

# MaaS-published URL is set
oc get llminferenceservice redhataigpt-oss-20b -n gpt-oss-model --context="$CTX" \
  -o jsonpath='{.status.url}'
# Expected: https://inference.maas.<CLUSTER_DOMAIN>/gpt-oss-model/redhataigpt-oss-20b
```

## What You Deployed

| Resource | Namespace | Purpose |
|----------|-----------|---------|
| Namespace `gpt-oss-model` | -- | Isolation for model serving resources |
| Secret `gpt-oss-20b-connection` | `gpt-oss-model` | OCI connection URI for model weights |
| LLMInferenceServiceConfig `redhataigpt-oss-20b` | `gpt-oss-model` | vLLM CUDA template (per-service copy) |
| LLMInferenceService `redhataigpt-oss-20b` | `gpt-oss-model` | llm-d model deployment (1 GPU), routed through the MaaS gateway |

---

**Next**: [Module 15 -- Model Endpoint](../15-model-endpoint/README.md)
