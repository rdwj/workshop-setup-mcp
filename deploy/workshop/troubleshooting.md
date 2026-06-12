# Troubleshooting

Common issues encountered during the workshop and how to resolve them.

---

## 504 Gateway Timeout on Tool Calls

**Symptom:** The agent connects to the MCP Gateway and discovers tools,
but every `tools/call` returns a 504 timeout.

**Cause:** The MCP broker's `privateHost` is missing the port. The router
tries port 80 (default) instead of 8080, and the connection times out.

**Fix:** Verify the MCPGatewayExtension has the port in `privateHost`:

```bash
oc get mcpgatewayextension mcp-gateway -n mcp-system -o jsonpath='{.spec.privateHost}'
```

It should end with `:8080`. If not, patch it:

```bash
oc patch mcpgatewayextension mcp-gateway -n mcp-system --type=merge -p '
  {"spec": {"privateHost": "mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080"}}'
oc rollout restart deployment/mcp-gateway -n mcp-system
```

---

## "Client is not connected" Errors

**Symptom:** The agent logs show `RuntimeError: Client is not connected.
Use the 'async with client:' context manager first.` Tool calls fail
but the agent pod is Running.

**Cause:** The agent's MCP session became stale. This happens when the
broker restarts (Module 5 broker restart, Module 6 wristband key patch)
after the agent has already connected.

**Fix:** Restart the agent to re-establish the MCP session:

```bash
oc rollout restart deployment/workshop-setup-mcp -n workshop-setup-mcp
```

---

## Broker Stuck in "GET requires Mcp-Session-Id" Loop

**Symptom:** The broker logs show a repeating error:

```
ERROR: failed to listen to server. retry in 1 second: request failed
with status 400: Bad Request: GET requires an Mcp-Session-Id header
```

**Cause:** The backend MCP server pod restarted, invalidating the
broker's MCP session with it.

**Fix:** Restart the broker, then restart the agent:

```bash
oc rollout restart deployment/mcp-gateway -n mcp-system
# Wait for the broker to reconnect to the backend
sleep 15
oc rollout restart deployment/workshop-setup-mcp -n workshop-setup-mcp
```

---

## Empty Tool List (0 Tools)

**Symptom:** The agent connects to the gateway but discovers 0 tools.
Or `tools/list` returns an empty list.

**Possible causes:**

1. **Broker hasn't been restarted since registration.** The broker
   doesn't auto-reload when the config Secret changes.

   ```bash
   oc rollout restart deployment/mcp-gateway -n mcp-system
   ```

2. **Missing `scope=openid groups` in token request.** Without the
   `groups` claim, the AuthPolicy Rego policy sees no groups and may
   default to an empty tool set. Decode the token to check:

   ```bash
   echo "$TOKEN" | cut -d. -f2 | python3 -c "
   import sys, base64, json
   p = sys.stdin.read().strip()
   p += '=' * (4 - len(p) % 4)
   data = json.loads(base64.urlsafe_b64decode(p))
   print('groups:', data.get('groups', 'MISSING'))
   "
   ```

3. **MCPServerRegistration not in the correct namespace.** The
   registration must be in the same namespace as the HTTPRoute and the
   MCP server.

---

## 401 Unauthorized on All Requests

**Symptom:** Every request to the gateway returns HTTP 401, even with a
valid-looking token.

**Possible causes:**

1. **AuthPolicy `issuerUrl` doesn't match Keycloak.** The issuer URL
   must match exactly, including the scheme and realm path. Check:

   ```bash
   oc get authpolicy mcp-gateway-auth -n mcp-system \
     -o jsonpath='{.spec.defaults.rules.authentication.keycloak-jwt.jwt.issuerUrl}'
   ```

   Compare with your Keycloak realm URL:

   ```bash
   echo "https://$(oc get route keycloak -n keycloak -o jsonpath='{.spec.host}')/realms/mcp-gateway"
   ```

2. **Token expired.** Default Keycloak access token lifetime is 5
   minutes. Increase it in Module 6 Step 11, or request a fresh token.

3. **Authorino not processing the policy.** Check Authorino logs:

   ```bash
   oc logs -l authorino-resource=authorino -n kuadrant-system --tail=50
   ```

---

## InstallPlan Stuck on "Requires Approval"

**Symptom:** An operator CSV never appears. `oc get installplan` shows
`APPROVED=false`.

**Cause:** Another operator in the namespace uses Manual approval mode.
OLM inherits the approval mode from the OperatorGroup.

**Fix:** Approve pending install plans:

```bash
for plan in $(oc get installplan -n openshift-operators \
  -o jsonpath='{.items[?(@.spec.approved==false)].metadata.name}'); do
  oc patch installplan "$plan" -n openshift-operators \
    --type=merge -p '{"spec":{"approved":true}}'
done
```

---

## Restart Order

When things go wrong, the general restart order is bottom-up:

1. **MCP server** (`mcp-ecosystem`) — if the backend is unhealthy
2. **Broker** (`mcp-system`) — to re-establish the session with the backend
3. **Agent** (`workshop-setup-mcp`) — to re-establish the session with the broker

```bash
oc rollout restart deployment/openshift-mcp-server -n mcp-ecosystem
sleep 15
oc rollout restart deployment/mcp-gateway -n mcp-system
sleep 15
oc rollout restart deployment/workshop-setup-mcp -n workshop-setup-mcp
```

Wait 15 seconds between each restart to let the upstream component
become healthy before the downstream one reconnects.
