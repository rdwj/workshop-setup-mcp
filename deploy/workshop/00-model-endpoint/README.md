# Module 0: Model Endpoint

Before starting the MCP Ecosystem modules, you need an OpenAI-compatible
model endpoint that supports tool calling. You will use this endpoint
throughout the workshop.

## Option A: Use a Remote Model (No GPU Required)

If you have access to a vLLM or OpenAI-compatible model served elsewhere,
set your endpoint:

```bash
export MODEL_ENDPOINT="https://<your-model-host>/v1"
export MODEL_NAME="<model-name>"
```

Verify it responds:

```bash
curl -sk "${MODEL_ENDPOINT}/models" | python3 -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin).get('data',[])]"
```

If the endpoint requires authentication, you may need to add an API key
to the agent configuration later.

## Option B: Deploy a Local Model (Requires GPU Node)

If your cluster has GPU nodes, deploy a vLLM-based model.

### Step 1: Scale up a GPU node (if needed)

Your cluster administrator may need to create a GPU MachineSet. This is
cluster-specific and typically takes 10-15 minutes for the node to provision.

### Step 2: Deploy the model

Apply the model deployment manifest:

```bash
oc apply -f gpt-oss-20b-deployment.yaml
```

Wait for the model pod to be ready (this downloads the model weights,
which can take 5-10 minutes):

```bash
oc get pods -n gpt-oss-model -w
```

### Step 3: Create an internal service

KServe auth is enabled by default on the predictor service. Create a
direct internal service that bypasses auth for in-cluster access:

```bash
cat <<'EOF' | oc apply -f -
apiVersion: v1
kind: Service
metadata:
  name: gpt-oss-20b-internal
  namespace: gpt-oss-model
spec:
  selector:
    serving.kserve.io/inferenceservice: redhataigpt-oss-20b
  ports:
    - port: 8080
      targetPort: 8080
EOF
```

### Step 4: Set your endpoint

```bash
export MODEL_ENDPOINT="http://gpt-oss-20b-internal.gpt-oss-model.svc.cluster.local:8080/v1"
export MODEL_NAME="redhataigpt-oss-20b"
```

## Verify

Regardless of which option you chose, verify the model endpoint responds:

```bash
curl -sk "${MODEL_ENDPOINT}/models"
```

Record your `MODEL_ENDPOINT` and `MODEL_NAME` -- you will use them in
Module 7 when configuring the agent.
