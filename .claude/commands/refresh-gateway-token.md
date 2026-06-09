# Refresh MCP Gateway Token

Acquire a fresh Keycloak JWT and update the MCP Gateway server configuration so Claude Code can connect with valid credentials.

## Process

### Step 1: Get a fresh token

Run the token acquisition script:

```
bash scripts/get-mcp-token.sh
```

If it fails, check that these env vars are set:
- `MCP_KC_URL` — Keycloak base URL
- `MCP_KC_CLIENT_SECRET` — Client secret for the mcp-gateway client
- `MCP_KC_USER` — Keycloak username (e.g., developer-a or developer-b)
- `MCP_KC_PASS` — Password for the user

### Step 2: Read the MCP gateway URL

Check the env var `MCP_GATEWAY_URL`. If not set, ask the user for the gateway URL. It should look like: `https://openshift.mcp.apps.cluster-xxx.opentlc.com/mcp`

### Step 3: Update MCP server config

Read `.claude/settings.local.json` (create it if it doesn't exist). Update or add the `mcp-gateway` entry under `mcpServers` with the fresh token:

```json
{
  "mcpServers": {
    "mcp-gateway": {
      "type": "streamable-http",
      "url": "<MCP_GATEWAY_URL>",
      "headers": {
        "Authorization": "Bearer <TOKEN_FROM_STEP_1>"
      }
    }
  }
}
```

Preserve any other existing settings in the file.

### Step 4: Report result

Tell the user:
- The token has been refreshed
- Which user identity was used (from `MCP_KC_USER`)
- They need to restart their Claude Code session for the new token to take effect
- The token expires in 1 hour — run `/refresh-gateway-token` again when needed
