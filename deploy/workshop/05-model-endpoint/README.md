# Module 5: Model Endpoint

Before starting the MCP Ecosystem modules, you need an OpenAI-compatible
model endpoint that supports tool calling. You will use this endpoint
throughout the workshop.

If you completed Modules 1--4, your on-cluster model is already running.
Otherwise, choose Option A below.

## If You Completed Modules 1--4: Use Your Deployed Model

Set your endpoint to the internal service created in Module 4:

```bash
export MODEL_ENDPOINT="http://gpt-oss-20b-test.gpt-oss-model.svc.cluster.local:8080/v1"
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

Verify it responds:

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
in Module 11 when configuring the agent.

---

**Next**: [Module 6 -- MCP Gateway](../06-mcp-gateway/README.md)
