#!/usr/bin/env bash
set -euo pipefail

CTX="${1:-${CTX:-}}"
if [ -z "$CTX" ]; then
  echo "Usage: $0 <kube-context>" >&2
  echo "  or set CTX environment variable" >&2
  exit 1
fi

# Discover Keycloak URL from the cluster
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" -o jsonpath='{.spec.host}')"

# Get admin credentials
ADMIN_USER=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" \
  -o jsonpath='{.data.username}' | base64 -d)
ADMIN_PASS=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" \
  -o jsonpath='{.data.password}' | base64 -d)

# Get admin token
ADMIN_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" \
  -d "username=${ADMIN_USER}" \
  -d "password=${ADMIN_PASS}" \
  -d "grant_type=password" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Get the mcp-gateway client UUID and secret
CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

CLIENT_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

# Acquire MCP Gateway token
MCP_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-gateway" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "grant_type=client_credentials" \
  -d "scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo >&2 "Token acquired (${#MCP_TOKEN} chars)"
echo "$MCP_TOKEN"
