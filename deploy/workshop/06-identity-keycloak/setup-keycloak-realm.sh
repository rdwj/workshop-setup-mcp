#!/usr/bin/env bash
# Configure Keycloak "mcp-gateway" realm with groups, MCP server clients,
# tool roles, users, and role assignments.
#
# Creates:
#   - mcp-gateway realm
#   - mcp-gateway client (service account enabled, direct access grants)
#   - console-oidc client (confidential, for OpenShift console OIDC login)
#   - oc-cli client (public, for OpenShift CLI OIDC login)
#   - Audience mapper on mcp-gateway client (for K8s API token validation)
#   - Groups: mcp-admins, mcp-users, mcp-github
#   - "groups" client scope with oidc-group-membership-mapper
#   - Assigns groups scope to mcp-gateway, console-oidc, and oc-cli clients
#   - Assigns built-in "roles" scope to the mcp-gateway client
#   - Puts mcp-gateway service account into mcp-admins
#   - Bearer-only MCP server clients with tool roles
#   - Workshop users (developer-a, developer-b) with group and role assignments
#
# Prerequisites:
#   - Keycloak running in 'keycloak' namespace with keycloak-initial-admin secret
#
# Usage:
#   export CTX="default/api-cluster-.../kube:admin"
#   export KEYCLOAK_URL="https://keycloak-keycloak.apps.cluster-..."
#   export CLUSTER_DOMAIN="apps.cluster-..."
#   bash setup-keycloak-realm.sh
set -euo pipefail

: "${CTX:?Set CTX to your kube context}"
: "${KEYCLOAK_URL:?Set KEYCLOAK_URL to your Keycloak base URL}"
: "${CLUSTER_DOMAIN:?Set CLUSTER_DOMAIN to your cluster apps domain}"

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

# Look up client UUID early -- needed for scope assignments and updates
CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

echo "--- Enabling direct access grants on mcp-gateway client ---"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X PUT \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}" \
  -d '{"clientId":"mcp-gateway","enabled":true,"serviceAccountsEnabled":true,"standardFlowEnabled":false,"directAccessGrantsEnabled":true}')
case "$HTTP" in
  204) echo "  Enabled direct access grants" ;;
  *)   echo "  ERROR: HTTP ${HTTP}" ;;
esac

echo "--- Adding audience mapper to mcp-gateway client ---"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/protocol-mappers/models" \
  -d '{
    "name": "aud-mcp-gateway",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {
      "included.client.audience": "mcp-gateway",
      "access.token.claim": "true",
      "id.token.claim": "false"
    }
  }')
case "$HTTP" in
  201) echo "  Created audience mapper" ;;
  409) echo "  Mapper exists" ;;
  *)   echo "  HTTP ${HTTP}" ;;
esac

echo "--- Creating console-oidc client ---"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients" \
  -d "{
    \"clientId\": \"console-oidc\",
    \"enabled\": true,
    \"publicClient\": false,
    \"standardFlowEnabled\": true,
    \"directAccessGrantsEnabled\": false,
    \"serviceAccountsEnabled\": false,
    \"redirectUris\": [\"https://console-openshift-console.${CLUSTER_DOMAIN}/auth/callback\"],
    \"webOrigins\": [\"https://console-openshift-console.${CLUSTER_DOMAIN}\"]
  }")
case "$HTTP" in
  201) echo "  Created client" ;;
  409) echo "  Client exists" ;;
  *)   echo "  ERROR: HTTP ${HTTP}" ;;
esac

echo "--- Creating oc-cli client ---"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients" \
  -d '{
    "clientId": "oc-cli",
    "enabled": true,
    "publicClient": true,
    "standardFlowEnabled": true,
    "directAccessGrantsEnabled": false,
    "serviceAccountsEnabled": false,
    "redirectUris": ["http://localhost:8080"]
  }')
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
GROUPS_SCOPE_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/client-scopes" \
  | python3 -c "import sys,json; scopes=json.load(sys.stdin); print([s['id'] for s in scopes if s['name']=='groups'][0])")

curl -sk -X PUT -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/default-client-scopes/${GROUPS_SCOPE_ID}"
echo "  Done"

echo "--- Assigning 'groups' scope to console-oidc and oc-cli clients ---"
CONSOLE_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=console-oidc" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])" 2>/dev/null)
OC_CLI_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=oc-cli" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])" 2>/dev/null)

if [ -n "$CONSOLE_UUID" ]; then
  curl -sk -X PUT -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CONSOLE_UUID}/default-client-scopes/${GROUPS_SCOPE_ID}"
  echo "  console-oidc: assigned"
fi
if [ -n "$OC_CLI_UUID" ]; then
  curl -sk -X PUT -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${OC_CLI_UUID}/default-client-scopes/${GROUPS_SCOPE_ID}"
  echo "  oc-cli: assigned"
fi

echo "--- Assigning built-in 'roles' scope to mcp-gateway client ---"
ROLES_SCOPE_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/client-scopes" \
  | python3 -c "import sys,json; scopes=json.load(sys.stdin); matches=[s['id'] for s in scopes if s['name']=='roles']; print(matches[0] if matches else '')")
if [ -n "$ROLES_SCOPE_ID" ]; then
  curl -sk -X PUT -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/default-client-scopes/${ROLES_SCOPE_ID}"
  echo "  Done"
else
  echo "  WARNING: built-in 'roles' scope not found"
fi

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

# ---------------------------------------------------------------------------
# MCP server clients (bearer-only) and tool roles
# ---------------------------------------------------------------------------

# Helper: create a bearer-only client and return its UUID
create_mcp_client() {
  local CLIENT_ID=$1
  echo "--- Creating bearer-only client: ${CLIENT_ID} ---" >&2
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients" \
    -d "{\"clientId\":\"${CLIENT_ID}\",\"enabled\":true,\"bearerOnly\":true,\"publicClient\":false}")
  case "$HTTP" in
    201) echo "  Created client" >&2 ;;
    409) echo "  Client exists" >&2 ;;
    *)   echo "  ERROR: HTTP ${HTTP}" >&2 ;;
  esac
  # Return UUID
  curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=${CLIENT_ID}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])"
}

# Helper: create roles on a client
create_client_roles() {
  local MCP_CLIENT_UUID=$1
  shift
  local ROLES=("$@")
  echo "  Creating ${#ROLES[@]} tool roles..." >&2
  for ROLE in "${ROLES[@]}"; do
    HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
      -H "Authorization: Bearer ${ADMIN_TOKEN}" \
      -H "Content-Type: application/json" \
      "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${MCP_CLIENT_UUID}/roles" \
      -d "{\"name\":\"${ROLE}\"}")
    case "$HTTP" in
      201) echo "    Created: ${ROLE}" >&2 ;;
      409) echo "    Exists:  ${ROLE}" >&2 ;;
      *)   echo "    ERROR creating ${ROLE}: HTTP ${HTTP}" >&2 ;;
    esac
  done
}

# OpenShift MCP server client
OCP_TOOLS=(
  configuration_view events_list namespaces_list nodes_log nodes_stats_summary
  nodes_top pods_get pods_list pods_list_in_namespace pods_log pods_top
  pods_run projects_list resources_get resources_list
)
OCP_CLIENT_UUID=$(create_mcp_client "mcp-ecosystem/openshift-mcp-server")
create_client_roles "$OCP_CLIENT_UUID" "${OCP_TOOLS[@]}"

# GitHub MCP server client
GH_TOOLS=(
  get_commit get_file_contents get_label get_latest_release get_me
  get_release_by_tag get_tag get_team_members get_teams issue_read
  list_branches list_commits list_issue_types list_issues list_pull_requests
  list_releases list_repository_collaborators list_tags pull_request_read
  search_code search_commits search_issues search_pull_requests
  search_repositories search_users
)
GH_CLIENT_UUID=$(create_mcp_client "mcp-ecosystem/github-mcp-server")
create_client_roles "$GH_CLIENT_UUID" "${GH_TOOLS[@]}"

# ---------------------------------------------------------------------------
# Workshop users
# ---------------------------------------------------------------------------

# Helper: create a user and return their UUID
create_user() {
  local USERNAME=$1 FIRST=$2 LAST=$3 EMAIL=$4 PASSWORD=$5
  echo "--- Creating user: ${USERNAME} ---" >&2
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users" \
    -d "{\"username\":\"${USERNAME}\",\"firstName\":\"${FIRST}\",\"lastName\":\"${LAST}\",\"email\":\"${EMAIL}\",\"enabled\":true,\"credentials\":[{\"type\":\"password\",\"value\":\"${PASSWORD}\",\"temporary\":false}]}")
  case "$HTTP" in
    201) echo "  Created user" >&2 ;;
    409) echo "  User exists" >&2 ;;
    *)   echo "  ERROR: HTTP ${HTTP}" >&2 ;;
  esac
  curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users?username=${USERNAME}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])"
}

# Helper: assign a user to a group
assign_group() {
  local USER_ID=$1 GROUP_NAME=$2
  local GROUP_ID
  GROUP_ID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/groups" \
    | python3 -c "import sys,json; groups=json.load(sys.stdin); print([g['id'] for g in groups if g['name']=='${GROUP_NAME}'][0])")
  curl -sk -X PUT -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users/${USER_ID}/groups/${GROUP_ID}"
  echo "  Assigned to ${GROUP_NAME}" >&2
}

# Helper: assign ALL client roles to a user
assign_all_roles() {
  local USER_ID=$1 MCP_CLIENT_UUID=$2 CLIENT_NAME=$3
  ROLES_JSON=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${MCP_CLIENT_UUID}/roles" 2>/dev/null)
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users/${USER_ID}/role-mappings/clients/${MCP_CLIENT_UUID}" \
    -d "${ROLES_JSON}")
  ROLE_COUNT=$(echo "$ROLES_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
  echo "  ${CLIENT_NAME}: assigned all ${ROLE_COUNT} roles (HTTP ${HTTP})" >&2
}

# Helper: assign a SUBSET of client roles to a user
assign_subset_roles() {
  local USER_ID=$1 MCP_CLIENT_UUID=$2 CLIENT_NAME=$3
  shift 3
  local TOOL_NAMES=("$@")
  ALL_ROLES=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${MCP_CLIENT_UUID}/roles" 2>/dev/null)
  SUBSET=$(python3 -c "
import json, sys
roles = json.loads(sys.stdin.read())
subset = set(sys.argv[1:])
filtered = [r for r in roles if r['name'] in subset]
print(json.dumps(filtered))
" "${TOOL_NAMES[@]}" <<< "$ALL_ROLES")
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    "${KEYCLOAK_URL}/admin/realms/mcp-gateway/users/${USER_ID}/role-mappings/clients/${MCP_CLIENT_UUID}" \
    -d "${SUBSET}")
  ROLE_COUNT=$(echo "$SUBSET" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
  echo "  ${CLIENT_NAME}: assigned ${ROLE_COUNT} roles (HTTP ${HTTP})" >&2
}

# Create users
DEV_A_ID=$(create_user "developer-a" "Developer" "A" "developer-a@workshop.local" "developer-a")
DEV_B_ID=$(create_user "developer-b" "Developer" "B" "developer-b@workshop.local" "developer-b")

# Assign users to groups
echo "--- Assigning users to groups ---"
assign_group "$DEV_A_ID" "mcp-admins"
assign_group "$DEV_B_ID" "mcp-users"

# Assign tool roles to developer-a (admin -- all roles on both clients)
echo "--- Assigning tool roles to developer-a ---"
assign_all_roles "$DEV_A_ID" "$OCP_CLIENT_UUID" "openshift-mcp-server"
assign_all_roles "$DEV_A_ID" "$GH_CLIENT_UUID" "github-mcp-server"

# Assign tool roles to developer-b (user -- subset on both clients)
echo "--- Assigning tool roles to developer-b ---"
OCP_USER_TOOLS=(
  namespaces_list pods_get pods_list pods_list_in_namespace pods_log
  projects_list resources_get resources_list
)
GH_USER_TOOLS=(
  get_file_contents get_me issue_read list_branches list_issues
  list_pull_requests list_releases pull_request_read search_code
  search_issues search_pull_requests search_repositories search_users
  get_commit list_commits
)
assign_subset_roles "$DEV_B_ID" "$OCP_CLIENT_UUID" "openshift-mcp-server" "${OCP_USER_TOOLS[@]}"
assign_subset_roles "$DEV_B_ID" "$GH_CLIENT_UUID" "github-mcp-server" "${GH_USER_TOOLS[@]}"

# Assign tool roles to the mcp-gateway service account (the agent's
# identity in Module 16). Group membership only drives VirtualMCPServer
# routing; without resource_access tool roles the wristband allowed-tools
# is empty and the broker filters the agent's tool list to ZERO.
echo "--- Assigning tool roles to mcp-gateway service account (agent identity) ---"
assign_all_roles "$SA_USER_ID" "$OCP_CLIENT_UUID" "openshift-mcp-server"
assign_all_roles "$SA_USER_ID" "$GH_CLIENT_UUID" "github-mcp-server"

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

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
ra = data.get('resource_access', {})
for client, info in ra.items():
    if client.startswith('mcp-ecosystem/'):
        roles = info.get('roles', [])
        print(f'  {client}: {len(roles)} tool roles')
"

echo "--- Verifying developer-a token (direct access grant) ---"
DEV_A_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-gateway&client_secret=${CLIENT_SECRET}&grant_type=password&username=developer-a&password=developer-a&scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "$DEV_A_TOKEN" | cut -d. -f2 | python3 -c "
import sys, base64, json
payload = sys.stdin.read().strip()
payload += '=' * (4 - len(payload) % 4)
data = json.loads(base64.urlsafe_b64decode(payload))
groups = data.get('groups', [])
print(f'  Groups: {groups}')
ra = data.get('resource_access', {})
for client, info in ra.items():
    if client.startswith('mcp-ecosystem/'):
        roles = info.get('roles', [])
        print(f'  {client}: {len(roles)} tool roles')
"
