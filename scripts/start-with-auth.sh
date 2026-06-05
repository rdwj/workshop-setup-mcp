#!/usr/bin/env bash
# Acquire Keycloak JWT and start the agent with MCP gateway auth
set -euo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:-}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-mcp-gateway}"
KEYCLOAK_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-}"
KEYCLOAK_CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:-}"

if [ -n "$KEYCLOAK_URL" ] && [ -n "$KEYCLOAK_CLIENT_ID" ] && [ -n "$KEYCLOAK_CLIENT_SECRET" ]; then
    echo "Acquiring JWT from Keycloak..."
    TOKEN_RESPONSE=$(curl -sk -X POST \
      "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
      -d "client_id=${KEYCLOAK_CLIENT_ID}" \
      -d "client_secret=${KEYCLOAK_CLIENT_SECRET}" \
      -d "grant_type=client_credentials" \
      -d "scope=openid groups" 2>/dev/null)

    MCP_AUTH_TOKEN=$(python3 -c "import json; print(json.loads('''${TOKEN_RESPONSE}''').get('access_token',''))" 2>/dev/null || echo "")

    if [ -n "$MCP_AUTH_TOKEN" ]; then
        export MCP_AUTH_TOKEN
        echo "JWT acquired (expires in 1 hour)"
    else
        echo "WARNING: Failed to acquire JWT. MCP gateway calls will be unauthenticated."
        echo "Response: $TOKEN_RESPONSE"
    fi
else
    echo "Keycloak credentials not configured. Running without MCP auth."
    echo "Set KEYCLOAK_URL, KEYCLOAK_CLIENT_ID, KEYCLOAK_CLIENT_SECRET to enable."
fi

exec python -m src.agent "$@"
