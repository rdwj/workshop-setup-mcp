#!/usr/bin/env bash
# Create Keycloak bearer-only client and tool roles for the calculus-helper MCP server.
# Assigns all roles to both workshop users initially.
#
# Prerequisites:
#   - Keycloak "mcp-gateway" realm exists (Module 10 setup-keycloak-realm.sh)
#   - Users developer-a and developer-b exist
#
# Usage:
#   export CTX="<kube-context>"
#   export KEYCLOAK_URL="https://keycloak-keycloak.apps.cluster-..."
#   bash setup-calculus-roles.sh
set -euo pipefail

: "${CTX:?Set CTX to your kube context}"
: "${KEYCLOAK_URL:?Set KEYCLOAK_URL to your Keycloak base URL}"

ADMIN_USER=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" -o jsonpath='{.data.username}' | base64 -d)
ADMIN_PASS=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" -o jsonpath='{.data.password}' | base64 -d)
ADMIN_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli&username=${ADMIN_USER}&password=${ADMIN_PASS}&grant_type=password" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

CLIENT_ID="mcp-ecosystem/calculus-helper"
TOOLS=(
  calculate_area_under_curve
  compute_derivative
  compute_integral
  compute_limit
  expand_series
  multivariable_calc
  solve_equation
  solve_ode
)

echo "--- Creating bearer-only client: ${CLIENT_ID} ---"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients" \
  -d "{\"clientId\":\"${CLIENT_ID}\",\"enabled\":true,\"bearerOnly\":true,\"publicClient\":false}")
case "$HTTP" in
  201) echo "  Created client" ;;
  409) echo "  Client exists" ;;
  *)   echo "  ERROR: HTTP ${HTTP}"; exit 1 ;;
esac

CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=${CLIENT_ID}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

echo "--- Creating ${#TOOLS[@]} tool roles ---"
for ROLE in "${TOOLS[@]}"; do
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/roles" \
    -d "{\"name\":\"${ROLE}\"}")
  case "$HTTP" in
    201) echo "  Created: ${ROLE}" ;;
    409) echo "  Exists:  ${ROLE}" ;;
    *)   echo "  ERROR creating ${ROLE}: HTTP ${HTTP}" ;;
  esac
done

echo "--- Assigning all roles to workshop users ---"
ROLES_JSON=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/roles" 2>/dev/null)

for USERNAME in developer-a developer-b; do
  USER_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users?username=${USERNAME}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users/${USER_ID}/role-mappings/clients/${CLIENT_UUID}" \
    -d "${ROLES_JSON}")
  echo "  ${USERNAME}: assigned all ${#TOOLS[@]} roles (HTTP ${HTTP})"
done

echo "--- Done ---"
echo "Both users now have all calculus-helper tool roles."
echo "To restrict expand_series to admins, remove that role from developer-b:"
echo "  1. GET the role: curl .../clients/${CLIENT_UUID}/roles/expand_series"
echo "  2. DELETE from user: curl -X DELETE .../users/<developer-b-id>/role-mappings/clients/${CLIENT_UUID} -d '[<role>]'"
