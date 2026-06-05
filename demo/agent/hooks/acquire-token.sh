#!/usr/bin/env bash
# pre_mcp_connect hook: acquire Keycloak JWT for MCP gateway auth.
# Prints MCP_AUTH_TOKEN=<jwt> to stdout so the framework sets the env var
# before connecting to the MCP server.
set -euo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:-}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-mcp-gateway}"
KEYCLOAK_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-}"
KEYCLOAK_CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:-}"

[ -n "$KEYCLOAK_URL" ] && [ -n "$KEYCLOAK_CLIENT_ID" ] && [ -n "$KEYCLOAK_CLIENT_SECRET" ] || exit 0

TOKEN=$(curl -sk -X POST \
  "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
  -d "client_id=${KEYCLOAK_CLIENT_ID}" \
  -d "client_secret=${KEYCLOAK_CLIENT_SECRET}" \
  -d "grant_type=client_credentials" \
  -d "scope=openid groups" 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null) || exit 0

echo "MCP_AUTH_TOKEN=${TOKEN}"
