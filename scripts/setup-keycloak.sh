#!/usr/bin/env bash
# Set up Keycloak realm and client for MCP Gateway auth demo
set -euo pipefail

CTX="${OC_CONTEXT:-}"
KEYCLOAK_NS="${KEYCLOAK_NS:-keycloak}"

if [ -z "$CTX" ]; then
    echo "Set OC_CONTEXT to your cluster context"
    exit 1
fi

echo "=== Keycloak MCP Realm Setup ==="

# Get Keycloak URL
KEYCLOAK_URL="https://$(oc get route keycloak -n "$KEYCLOAK_NS" --context="$CTX" -o jsonpath='{.spec.host}')"
echo "Keycloak URL: $KEYCLOAK_URL"

# Get admin credentials
ADMIN_USER=$(oc get secret keycloak-initial-admin -n "$KEYCLOAK_NS" --context="$CTX" -o jsonpath='{.data.username}' | base64 -d)
ADMIN_PASS=$(oc get secret keycloak-initial-admin -n "$KEYCLOAK_NS" --context="$CTX" -o jsonpath='{.data.password}' | base64 -d)

# Get admin token
TOKEN=$(curl -s -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" \
  -d "username=${ADMIN_USER}" \
  -d "password=${ADMIN_PASS}" \
  -d "grant_type=password" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Check if realm exists
EXISTING=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway")

if [ "$EXISTING" = "200" ]; then
    echo "Realm 'mcp' already exists. Skipping creation."
else
    echo "Creating realm 'mcp'..."
    curl -s -X POST -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json" \
      "${KEYCLOAK_URL}/admin/realms" \
      -d '{"realm":"mcp","enabled":true}'
    echo "Realm created."
fi

# Create confidential client
echo "Creating client 'mcp-gateway'..."
curl -s -X POST -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients" \
  -d '{
    "clientId":"mcp-gateway",
    "name":"MCP Agent Client",
    "enabled":true,
    "clientAuthenticatorType":"client-secret",
    "serviceAccountsEnabled":true,
    "standardFlowEnabled":false,
    "directAccessGrantsEnabled":true,
    "protocol":"openid-connect",
    "publicClient":false
  }' 2>/dev/null || echo "(may already exist)"

# Get client secret
CLIENT_UUID=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-gateway" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

CLIENT_SECRET=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/client-secret" | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

# Create user-level client (mcp-users group, read-only tools)
echo "Creating client 'mcp-user-agent'..."
curl -s -X POST -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients" \
  -d '{
    "clientId":"mcp-user-agent",
    "name":"MCP User Agent (read-only)",
    "enabled":true,
    "clientAuthenticatorType":"client-secret",
    "serviceAccountsEnabled":true,
    "standardFlowEnabled":false,
    "directAccessGrantsEnabled":true,
    "protocol":"openid-connect",
    "publicClient":false
  }' 2>/dev/null || echo "(may already exist)"

# Get user client secret
USER_CLIENT_UUID=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-user-agent" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

USER_CLIENT_SECRET=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${USER_CLIENT_UUID}/client-secret" | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

# Note: The service account for mcp-user-agent must be added to the
# 'mcp-users' group in Keycloak for wristband tool filtering to work.
# The mcp-gateway client's service account should be in the 'mcp-admins' group.
# Group assignment is done in the Keycloak admin console under:
#   Users -> service-account-mcp-user-agent -> Groups -> Join Group

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Keycloak URL:  ${KEYCLOAK_URL}"
echo "Realm:         mcp-gateway"
echo "Issuer URL:    ${KEYCLOAK_URL}/realms/mcp-gateway"
echo ""
echo "Admin client (mcp-admins group, all tools):"
echo "  Client ID:     mcp-gateway"
echo "  Client Secret: ${CLIENT_SECRET}"
echo ""
echo "User client (mcp-users group, read-only tools):"
echo "  Client ID:     mcp-user-agent"
echo "  Client Secret: ${USER_CLIENT_SECRET}"
echo ""
echo "NOTE: Assign service accounts to groups in the Keycloak admin console:"
echo "  - service-account-mcp-gateway    -> mcp-admins group"
echo "  - service-account-mcp-user-agent -> mcp-users group"
echo ""
echo "Export these for the demo:"
echo "  export KEYCLOAK_URL=${KEYCLOAK_URL}"
echo "  export CLIENT_SECRET=${CLIENT_SECRET}"
echo "  export USER_CLIENT_SECRET=${USER_CLIENT_SECRET}"
echo "  export MCP_GATEWAY_URL=https://<gateway-route>/mcp"
