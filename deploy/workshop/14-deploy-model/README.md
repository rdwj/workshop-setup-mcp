# Module 14: Deploy a Model from the Catalog (Optional Model Track)

Deploy the Red Hat gpt-oss-20b model using KServe on your GPU node. The
model will appear in the RHOAI Dashboard under Deployed Models and serve
an OpenAI-compatible API.

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

    The dashboard creates the ServingRuntime and InferenceService for you.
    Skip to Step 2 to monitor the deployment.

=== "Approach B: CLI"

    Apply the deployment manifest, which creates the namespace, OCI
    connection secret, ServiceAccount, ServingRuntime, and InferenceService:

    ```bash
    oc apply -f gpt-oss-20b-deployment.yaml --context="$CTX"
    ```

    The manifest deploys five resources:

    - **Namespace** `gpt-oss-model` with the RHOAI dashboard label
    - **Secret** `gpt-oss-20b-connection` pointing to the OCI model image
      (`oci://registry.redhat.io/rhelai1/modelcar-gpt-oss-20b:1.5`)
    - **ServiceAccount** `redhataigpt-oss-20b-sa` for pod identity
    - **ServingRuntime** `redhataigpt-oss-20b` running vLLM with CUDA
    - **InferenceService** `redhataigpt-oss-20b` with 1 GPU, KServe auth
      enabled, and the `nvidia-gpu` HardwareProfile

## Step 2: Wait for the Model

The KServe controller schedules the predictor pod on your GPU node. The
pod downloads model weights from the OCI registry, which takes 5--10
minutes:

```bash
oc get pods -n gpt-oss-model --context="$CTX" -w
```

Wait until the pod status changes from `Init` to `Running` and all
containers are ready.

!!! note "Model weight download"
    The gpt-oss-20b model weights are pulled from
    `registry.redhat.io/rhelai1/modelcar-gpt-oss-20b:1.5` as an OCI
    artifact. An init container handles the download before vLLM starts.
    If the pod stays in `Init` for more than 10 minutes, check the init
    container logs:
    `oc logs -n gpt-oss-model --context="$CTX" -l serving.kserve.io/inferenceservice=redhataigpt-oss-20b -c modelcar-init`

## Step 3: Verify the InferenceService

```bash
oc get inferenceservice redhataigpt-oss-20b -n gpt-oss-model --context="$CTX"
```

The `READY` column should show `True`. This means the model is loaded
and serving requests.

## Step 4: Test the Model Endpoint

Create an internal service to test the model directly (bypassing KServe
auth for in-cluster testing):

```bash
cat <<'EOF' | oc apply --context="$CTX" -f -
apiVersion: v1
kind: Service
metadata:
  name: gpt-oss-20b-test
  namespace: gpt-oss-model
spec:
  selector:
    serving.kserve.io/inferenceservice: redhataigpt-oss-20b
  ports:
    - port: 8080
      targetPort: 8080
EOF
```

Test from within the cluster:

```bash
oc run curl-test -n gpt-oss-model --context="$CTX" --rm -it \
  --image=registry.redhat.io/ubi9/ubi-minimal:latest \
  --restart=Never -- \
  curl -s http://gpt-oss-20b-test:8080/v1/models | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('data', []):
    print(m['id'])
"
```

You should see `redhataigpt-oss-20b` in the output.

!!! note "Check the RHOAI Dashboard"
    Open the RHOAI Dashboard and navigate to **Deployed Models**. Your
    gpt-oss-20b model should appear with a green status indicator.

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
# InferenceService is ready
oc get inferenceservice redhataigpt-oss-20b -n gpt-oss-model --context="$CTX" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
# Expected: True

# Pod is running on GPU node
oc get pods -n gpt-oss-model --context="$CTX" \
  -o jsonpath='{.items[0].spec.nodeName}'
# Expected: your GPU node name

# ServingRuntime exists
oc get servingruntime redhataigpt-oss-20b -n gpt-oss-model --context="$CTX" \
  -o jsonpath='{.metadata.name}'
# Expected: redhataigpt-oss-20b
```

## What You Deployed

| Resource | Namespace | Purpose |
|----------|-----------|---------|
| Namespace `gpt-oss-model` | -- | Isolation for model serving resources |
| Secret `gpt-oss-20b-connection` | `gpt-oss-model` | OCI connection URI for model weights |
| ServiceAccount `redhataigpt-oss-20b-sa` | `gpt-oss-model` | Pod identity for the model server |
| ServingRuntime `redhataigpt-oss-20b` | `gpt-oss-model` | vLLM CUDA runtime configuration |
| InferenceService `redhataigpt-oss-20b` | `gpt-oss-model` | KServe model deployment (1 GPU, auth enabled) |
| Service `gpt-oss-20b-test` | `gpt-oss-model` | Internal test endpoint bypassing KServe auth |

---

**Next**: [Module 15 -- Model Endpoint](../15-model-endpoint/README.md)
