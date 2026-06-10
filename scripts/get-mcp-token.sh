#!/usr/bin/env bash
# Acquire a Keycloak JWT for the MCP Gateway.
#
# Reads credentials from environment variables and outputs a Bearer token.
# Intended to be called by the /refresh-gateway-token Claude Code command.
#
# Required env vars:
#   MCP_KC_URL          Keycloak base URL (e.g., https://keycloak-keycloak.apps.cluster-xxx)
#   MCP_KC_CLIENT_SECRET  Client secret for the mcp-gateway Keycloak client
#   MCP_KC_USER         Username (e.g., developer-a)
#   MCP_KC_PASS     Password for the user
#
# Optional env vars:
#   MCP_KC_REALM        Realm name (default: mcp-gateway)
#   MCP_KC_CLIENT_ID    Client ID (default: mcp-gateway)
set -euo pipefail

: "${MCP_KC_URL:?Set MCP_KC_URL to your Keycloak base URL}"
: "${MCP_KC_CLIENT_SECRET:?Set MCP_KC_CLIENT_SECRET}"
: "${MCP_KC_USER:?Set MCP_KC_USER to your Keycloak username}"
: "${MCP_KC_PASS:?Set MCP_KC_PASS}"

REALM="${MCP_KC_REALM:-mcp-gateway}"
CLIENT_ID="${MCP_KC_CLIENT_ID:-mcp-gateway}"

RESPONSE=$(curl -sk -X POST \
  "${MCP_KC_URL}/realms/${REALM}/protocol/openid-connect/token" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${MCP_KC_CLIENT_SECRET}" \
  -d "grant_type=password" \
  -d "username=${MCP_KC_USER}" \
  -d "password=${MCP_KC_PASS}" \
  -d "scope=openid groups")

TOKEN=$(echo "$RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'access_token' in d:
    print(d['access_token'])
else:
    print(f'Keycloak error: {d.get(\"error_description\", d)}', file=sys.stderr)
    sys.exit(1)
")

echo "$TOKEN"
