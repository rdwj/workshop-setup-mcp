#!/usr/bin/env bash
# MCP Gateway Auth Demo
# Demonstrates authenticated vs unauthenticated access to MCP tools
set -euo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:-https://keycloak-keycloak.apps.cluster.example.com}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-mcp-gateway}"
CLIENT_ID="${CLIENT_ID:-mcp-gateway}"
CLIENT_SECRET="${CLIENT_SECRET:-}"
GATEWAY_URL="${MCP_GATEWAY_URL:-}"

echo "=== MCP Gateway Auth Demo ==="
echo ""

# Step 1: Test without auth
echo "--- Step 1: Attempt without authentication ---"
echo "Gateway: ${GATEWAY_URL}"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"demo","version":"1.0"}}}' \
  "${GATEWAY_URL}" 2>/dev/null) || HTTP_CODE="000"

if [ "$HTTP_CODE" = "403" ] || [ "$HTTP_CODE" = "401" ]; then
    echo "DENIED (HTTP $HTTP_CODE) — auth is enforced"
else
    echo "ALLOWED (HTTP $HTTP_CODE) — auth is NOT enforced"
fi
echo ""

# Step 2: Get token from Keycloak
echo "--- Step 2: Obtain JWT from Keycloak ---"
if [ -z "$CLIENT_SECRET" ]; then
    echo "CLIENT_SECRET not set. Skipping authenticated test."
    echo "Set: export CLIENT_SECRET=<your-secret>"
    exit 0
fi

TOKEN=$(curl -s -X POST \
  "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "grant_type=client_credentials" \
  -d "scope=openid" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null) || TOKEN=""

if [ -z "$TOKEN" ]; then
    echo "Failed to obtain token."
    exit 1
fi
echo "Token obtained: ${TOKEN:0:20}..."
echo ""

# Step 3: Initialize with auth (capture session ID from response headers)
echo "--- Step 3: Initialize with authentication ---"
curl -s -D /tmp/mcp-demo-headers.txt -o /tmp/mcp-demo-response.json \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"demo","version":"1.0"}}}' \
  "${GATEWAY_URL}" 2>/dev/null

HTTP_CODE=$(grep -i "^HTTP/" /tmp/mcp-demo-headers.txt | tail -1 | awk '{print $2}')
SESSION_ID=$(grep -i "^mcp-session-id:" /tmp/mcp-demo-headers.txt | sed 's/[^:]*: //' | tr -d '\r\n')

if [ "${HTTP_CODE}" = "200" ]; then
    echo "SUCCESS (HTTP 200) — authenticated access granted"
    echo "Session ID: ${SESSION_ID:0:30}..."
    python3 -m json.tool /tmp/mcp-demo-response.json 2>/dev/null
else
    echo "FAILED (HTTP ${HTTP_CODE})"
    cat /tmp/mcp-demo-response.json 2>/dev/null
    exit 1
fi
echo ""

# Step 4: List tools using the session
echo "--- Step 4: List available tools ---"
curl -s -o /tmp/mcp-demo-tools.json \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Mcp-Session-Id: ${SESSION_ID}" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  "${GATEWAY_URL}" 2>/dev/null

python3 -c "
import json
with open('/tmp/mcp-demo-tools.json') as f:
    data = json.load(f)
tools = data.get('result', {}).get('tools', [])
print(f'{len(tools)} tools available:')
for t in tools:
    print(f'  - {t[\"name\"]}: {t.get(\"description\",\"\")[:80]}')
" 2>/dev/null || cat /tmp/mcp-demo-tools.json

# Step 5: Call a tool via the gateway
# Note: Tool calls through the gateway may fail if the broker's ext_proc
# routing creates a new backend session that loses the SA token context.
# This is a known broker routing issue, not an auth issue.
echo "--- Step 5: Call a tool (openshift_namespaces_list) ---"
curl -s -o /tmp/mcp-demo-call.json \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Mcp-Session-Id: ${SESSION_ID}" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"openshift_namespaces_list","arguments":{}}}' \
  "${GATEWAY_URL}" 2>/dev/null

python3 -c "
import json
with open('/tmp/mcp-demo-call.json') as f:
    data = json.load(f)
result = data.get('result', {})
content = result.get('content', [])
if content:
    # Tool results come as text content items
    for item in content:
        text = item.get('text', '')
        # Try to parse and summarize if it's JSON
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                print(f'Cluster has {len(parsed)} namespaces (showing first 10):')
                for ns in parsed[:10]:
                    name = ns.get('name', ns) if isinstance(ns, dict) else ns
                    print(f'  - {name}')
                if len(parsed) > 10:
                    print(f'  ... and {len(parsed) - 10} more')
            else:
                print(json.dumps(parsed, indent=2)[:500])
        except (json.JSONDecodeError, TypeError):
            print(text[:500])
else:
    error = data.get('error', {})
    if error:
        print(f'Error: {error.get(\"message\", data)}')
    else:
        print(json.dumps(data, indent=2)[:500])
" 2>/dev/null || cat /tmp/mcp-demo-call.json
echo ""

# Step 6: Admin vs User tool filtering (wristband-based access control)
echo "--- Step 6: Admin vs User tool filtering ---"
echo ""
ADMIN_TOOL_COUNT=$(python3 -c "
import json
with open('/tmp/mcp-demo-tools.json') as f:
    data = json.load(f)
print(len(data.get('result', {}).get('tools', [])))
" 2>/dev/null)
echo "Admin identity (${CLIENT_ID}): ${ADMIN_TOOL_COUNT} tools"
echo ""

# Try user-level identity if configured
USER_CLIENT_ID="${USER_CLIENT_ID:-mcp-user-agent}"
USER_CLIENT_SECRET="${USER_CLIENT_SECRET:-}"

if [ -n "$USER_CLIENT_SECRET" ]; then
    echo "Obtaining token for user identity (${USER_CLIENT_ID})..."
    USER_TOKEN=$(curl -s -X POST \
      "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
      -d "client_id=${USER_CLIENT_ID}" \
      -d "client_secret=${USER_CLIENT_SECRET}" \
      -d "grant_type=client_credentials" \
      -d "scope=openid" \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null) || USER_TOKEN=""

    if [ -n "$USER_TOKEN" ]; then
        echo "User token obtained: ${USER_TOKEN:0:20}..."

        # Initialize a new session with user token
        curl -s -D /tmp/mcp-demo-user-headers.txt -o /tmp/mcp-demo-user-init.json \
          -H "Content-Type: application/json" \
          -H "Authorization: Bearer ${USER_TOKEN}" \
          -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"demo-user","version":"1.0"}}}' \
          "${GATEWAY_URL}" 2>/dev/null

        USER_SESSION_ID=$(grep -i "^mcp-session-id:" /tmp/mcp-demo-user-headers.txt | sed 's/[^:]*: //' | tr -d '\r\n')

        # List tools with user identity
        curl -s -o /tmp/mcp-demo-user-tools.json \
          -H "Content-Type: application/json" \
          -H "Authorization: Bearer ${USER_TOKEN}" \
          -H "Mcp-Session-Id: ${USER_SESSION_ID}" \
          -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
          "${GATEWAY_URL}" 2>/dev/null

        python3 -c "
import json
with open('/tmp/mcp-demo-user-tools.json') as f:
    data = json.load(f)
tools = data.get('result', {}).get('tools', [])
print(f'User identity (mcp-user-agent): {len(tools)} tools')
for t in tools:
    print(f'  - {t[\"name\"]}')

# Show what the admin has that the user doesn't
with open('/tmp/mcp-demo-tools.json') as f:
    admin_data = json.load(f)
admin_tools = {t['name'] for t in admin_data.get('result', {}).get('tools', [])}
user_tools = {t['name'] for t in tools}
admin_only = sorted(admin_tools - user_tools)
if admin_only:
    print(f'')
    print(f'Admin-only tools ({len(admin_only)}):')
    for t in admin_only:
        print(f'  - {t}')
" 2>/dev/null || cat /tmp/mcp-demo-user-tools.json
    else
        echo "Failed to obtain user token."
    fi
else
    echo "User identity not configured. To demo tool filtering:"
    echo "  1. Create a Keycloak client 'mcp-user-agent' in the mcp-gateway realm"
    echo "  2. Add it to the 'mcp-users' group (read-only: 8 tools)"
    echo "  3. Export: USER_CLIENT_SECRET=<secret>"
    echo ""
    echo "Groups and tool counts:"
    echo "  mcp-admins  -> 14 tools (all openshift_* tools)"
    echo "  mcp-users   ->  8 tools (read-only subset)"
fi

echo ""
echo "=== Demo Complete ==="
