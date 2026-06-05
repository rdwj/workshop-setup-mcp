#!/usr/bin/env bash
# Configure Keycloak "mcp-gateway" realm with groups and group-membership mapper.
#
# Creates:
#   - mcp-gateway realm
#   - mcp-gateway client (service account enabled)
#   - Groups: mcp-admins, mcp-users, mcp-github
#   - "groups" client scope with oidc-group-membership-mapper
#   - Assigns groups scope to the mcp-gateway client
#   - Puts mcp-gateway service account into mcp-admins
#
# Prerequisites:
#   - Keycloak running in 'keycloak' namespace with keycloak-initial-admin secret
#
# Usage:
#   export CTX="default/api-cluster-.../kube:admin"
#   export KEYCLOAK_URL="https://keycloak-keycloak.apps.cluster-..."
#   bash setup-keycloak-realm.sh
set -euo pipefail

: "${CTX:?Set CTX to your kube context}"
: "${KEYCLOAK_URL:?Set KEYCLOAK_URL to your Keycloak base URL}"

ADMIN_USER=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" -o jsonpath='{.data.username}' | base64 -d)
ADMIN_PASS=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" -o jsonpath='{.data.password}' | base64 -d)
ADMIN_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli&username=${ADMIN_USER}&password=${ADMIN_PASS}&grant_type=password" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "--- Creating mcp-gateway realm ---"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms" \
  -d '{"realm":"mcp-gateway","enabled":true,"displayName":"MCP Gateway"}')
case "$HTTP" in
  201) echo "  Created realm" ;;
  409) echo "  Realm exists" ;;
  *)   echo "  ERROR: HTTP ${HTTP}"; exit 1 ;;
esac

echo "--- Creating mcp-gateway client ---"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients" \
  -d '{"clientId":"mcp-gateway","enabled":true,"serviceAccountsEnabled":true,"standardFlowEnabled":false,"directAccessGrantsEnabled":false}')
case "$HTTP" in
  201) echo "  Created client" ;;
  409) echo "  Client exists" ;;
  *)   echo "  ERROR: HTTP ${HTTP}" ;;
esac

echo "--- Creating groups ---"
for GROUP in mcp-admins mcp-users mcp-github; do
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/groups" \
    -d "{\"name\":\"${GROUP}\"}")
  case "$HTTP" in
    201) echo "  Created: ${GROUP}" ;;
    409) echo "  Exists:  ${GROUP}" ;;
    *)   echo "  ERROR creating ${GROUP}: HTTP ${HTTP}" ;;
  esac
done

echo "--- Creating 'groups' client scope with mapper ---"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/client-scopes" \
  -d '{
    "name": "groups",
    "protocol": "openid-connect",
    "protocolMappers": [{
      "name": "group-mapper",
      "protocol": "openid-connect",
      "protocolMapper": "oidc-group-membership-mapper",
      "config": {
        "access.token.claim": "true",
        "claim.name": "groups",
        "id.token.claim": "true",
        "full.path": "false",
        "userinfo.token.claim": "true"
      }
    }]
  }')
echo "  groups scope: HTTP ${HTTP}"

echo "--- Assigning 'groups' scope to mcp-gateway client ---"
CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

GROUPS_SCOPE_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/client-scopes" \
  | python3 -c "import sys,json; scopes=json.load(sys.stdin); print([s['id'] for s in scopes if s['name']=='groups'][0])")

curl -sk -X PUT -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/default-client-scopes/${GROUPS_SCOPE_ID}"
echo "  Done"

echo "--- Assigning mcp-gateway service account to mcp-admins ---"
SA_USER_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users?username=service-account-mcp-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

ADMINS_GROUP_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/groups" \
  | python3 -c "import sys,json; groups=json.load(sys.stdin); print([g['id'] for g in groups if g['name']=='mcp-admins'][0])")

curl -sk -X PUT -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users/${SA_USER_ID}/groups/${ADMINS_GROUP_ID}"
echo "  Done"

echo "--- Verifying groups claim in token ---"
CLIENT_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

TEST_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-gateway&client_secret=${CLIENT_SECRET}&grant_type=client_credentials&scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "$TEST_TOKEN" | cut -d. -f2 | python3 -c "
import sys, base64, json
payload = sys.stdin.read().strip()
payload += '=' * (4 - len(payload) % 4)
data = json.loads(base64.urlsafe_b64decode(payload))
groups = data.get('groups', [])
print(f'  Groups claim: {groups}')
if 'mcp-admins' in groups:
    print('  OK: mcp-gateway SA is in mcp-admins')
else:
    print('  WARNING: mcp-gateway SA is NOT in mcp-admins')
"
