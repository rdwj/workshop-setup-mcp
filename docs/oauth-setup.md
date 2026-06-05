# OAuth Authentication Setup for MCP Gateway

The workshop-setup-mcp agent supports OAuth2 client credentials authentication to the MCP Gateway via Keycloak.

## How it works

1. **Startup wrapper**: The container's `CMD` runs `/scripts/start-with-auth.sh` instead of directly starting the agent
2. **Token acquisition**: If Keycloak credentials are configured, the wrapper:
   - Makes a POST request to Keycloak's token endpoint
   - Requests a JWT using the `client_credentials` grant type
   - Exports the token as `MCP_AUTH_TOKEN` environment variable
3. **Agent startup**: The wrapper then starts the agent with `python -m src.agent`
4. **MCP connection**: The agent reads `${MCP_AUTH_TOKEN}` from the environment and includes it in the `Authorization: Bearer` header when connecting to the MCP Gateway

## Configuration

### Environment Variables

Set these in `chart/values.yaml` under the `config` section:

```yaml
config:
  KEYCLOAK_URL: https://keycloak.example.com
  KEYCLOAK_REALM: mcp-gateway
  KEYCLOAK_CLIENT_ID: workshop-agent
  KEYCLOAK_CLIENT_SECRET: <your-client-secret>
  MCP_GATEWAY_URL: http://mcp-gateway-<gatewayclass-name>.<gateway-namespace>.svc.cluster.local:8080/mcp
```

**Important:** Use the Istio gateway service (`mcp-gateway-<gatewayclass-name>`), not the broker. On RHOAI, this is typically `mcp-gateway-data-science-gateway-class`. Connecting directly to the broker bypasses all gateway-level authorization. See `docs/MCP-Ecosystem/09-best-practices.md` section 9.1.3.

**Note**: For production deployments, use a Secret for `KEYCLOAK_CLIENT_SECRET`:

```yaml
env:
  - name: KEYCLOAK_CLIENT_SECRET
    valueFrom:
      secretKeyRef:
        name: keycloak-credentials
        key: client-secret
```

### Token Lifetime

The JWT token has a 1-hour expiration. The current implementation acquires the token once at container startup. For long-running agents, consider:

- Restarting pods hourly (acceptable for stateless agents)
- Implementing token refresh in the wrapper script (check expiration and re-acquire as needed)
- Using a sidecar that manages token rotation

## Testing

### Verify token acquisition

Check the container logs after deployment:

```bash
oc logs deployment/workshop-setup-mcp -n workshop-demo
```

You should see:
```
Acquiring JWT from Keycloak...
JWT acquired (expires in 1 hour)
```

### Without credentials

If credentials are not configured, the wrapper logs:
```
Keycloak credentials not configured. Running without MCP auth.
Set KEYCLOAK_URL, KEYCLOAK_CLIENT_ID, KEYCLOAK_CLIENT_SECRET to enable.
```

The agent will still start but MCP Gateway calls will fail if the gateway requires authentication.

## Troubleshooting

### Token acquisition fails

If you see:
```
WARNING: Failed to acquire JWT. MCP gateway calls will be unauthenticated.
Response: {"error":"invalid_client",...}
```

Check:
- KEYCLOAK_CLIENT_ID and KEYCLOAK_CLIENT_SECRET are correct
- The client exists in the specified realm
- The client has "Client authentication" enabled (confidential client)
- Service account roles are configured if required by the gateway

### MCP Gateway returns 401

If the agent starts but MCP calls fail with 401:
- Verify the token is being set: `oc exec deployment/workshop-setup-mcp -- env | grep MCP_AUTH_TOKEN`
- Check the token hasn't expired (re-deploy if pod has been running >1 hour)
- Verify the MCP Gateway's expected audience matches what Keycloak issues

## Manual token testing

To manually test token acquisition:

```bash
curl -X POST https://keycloak.example.com/realms/mcp-gateway/protocol/openid-connect/token \
  -d "client_id=workshop-agent" \
  -d "client_secret=YOUR_SECRET" \
  -d "grant_type=client_credentials" \
  -d "scope=openid groups"
```

Expected response:
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_in": 3600,
  "token_type": "Bearer",
  ...
}
```
