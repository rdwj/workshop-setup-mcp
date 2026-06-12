# Module 15: Model Endpoint (Optional Model Track)

Before starting the MCP Ecosystem modules, you need an OpenAI-compatible
model endpoint that supports tool calling. You will use this endpoint
throughout the workshop.

If you completed Modules 12--14, your on-cluster model is already running.
Otherwise, choose Option A below.

## If You Completed Modules 12--14: Use Your Deployed Model

Set your endpoint to the workload Service KServe created alongside the
LLMInferenceService in Module 14 (verify with
`oc get svc redhataigpt-oss-20b-kserve-workload-svc -n gpt-oss-model`):

```bash
export MODEL_ENDPOINT="http://redhataigpt-oss-20b-kserve-workload-svc.gpt-oss-model.svc.cluster.local:8000/v1"
export MODEL_NAME="redhataigpt-oss-20b"
```

No API key is needed for the internal service:

```bash
export OPENAI_API_KEY="not-required"
```

Skip to the [Verify](#verify) section below.

## Option A: Use a Remote Model (No GPU Required)

If you have access to a vLLM or OpenAI-compatible model served elsewhere,
set your endpoint:

```bash
export MODEL_ENDPOINT="https://<your-model-host>/v1"
export MODEL_NAME="<model-name>"
```

If the endpoint requires a bearer token or API key:

```bash
export OPENAI_API_KEY="<your-api-key-or-token>"
```

Verify it responds. The on-cluster URL is only reachable from inside the
cluster, so run the check in a throwaway pod (no `-t`, and grep-based
extraction — `oc run --rm` mixes lifecycle messages into the stream and
breaks strict JSON parsing):

```bash
oc run model-check -n gpt-oss-model --context="$CTX" --rm -i \
  --image=registry.redhat.io/ubi9/ubi-minimal:latest --restart=Never -- \
  curl -s "${MODEL_ENDPOINT}/models" \
  | grep -o '"id":"[^"]*"'
# Expected: "id":"redhataigpt-oss-20b"
```

For an **external** endpoint, plain curl from your workstation works:

```bash
curl -sk -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  "${MODEL_ENDPOINT}/models" \
  | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for m in data.get('data', []):
        print(m['id'])
except (json.JSONDecodeError, KeyError):
    print('ERROR: endpoint did not return valid JSON. Check MODEL_ENDPOINT URL.', file=sys.stderr)
    sys.exit(1)
"
```

If the endpoint does not require authentication, you can omit the
`OPENAI_API_KEY` export and the `-H` flag above.

## Verify

Regardless of which option you chose, verify the model endpoint responds.
If you set `OPENAI_API_KEY` above, include the auth header; otherwise
omit it:

```bash
curl -sk ${OPENAI_API_KEY:+-H "Authorization: Bearer ${OPENAI_API_KEY}"} \
  "${MODEL_ENDPOINT}/models" \
  | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for m in data.get('data', []):
        print(m['id'])
except (json.JSONDecodeError, KeyError):
    print('ERROR: endpoint did not return valid JSON. Check MODEL_ENDPOINT URL.', file=sys.stderr)
    sys.exit(1)
"
```

You should see a list of model IDs. Record your `MODEL_ENDPOINT`,
`MODEL_NAME`, and `OPENAI_API_KEY` (if applicable) -- you will use them
in Module 16 when configuring the agent.

---

**Next**: [Module 16 -- Deploy the Agent Stack](../16-deploy-agent/README.md)
