# Module 10: Identity and Authentication

Set up Keycloak-based JWT authentication for the MCP Gateway with
wristband-signed tokens -- short-lived signed JWTs that the broker trusts for
tool-level authorization -- encoding per-group tool permissions.

After this module, the gateway will:
- Require a valid JWT from Keycloak on every request
- Map Keycloak groups to tool permission sets via OPA Rego policy
- Issue a short-lived wristband token containing an `allowed-tools` claim
- Return 401 for unauthenticated requests

**Time:** 30--45 minutes

**Prerequisites:**
- Modules 2, 6-9 complete (gateway infrastructure, MCP Gateway, MCP server prerequisites, MCP server, registration)
- `openssl` available on your workstation

> **Working directory:**
>
> ```bash
> cd deploy/workshop/10-identity-auth
> ```

> **Note:** The RHBK operator may inherit Manual InstallPlan approval from
> other operators on the cluster. If the CSV doesn't appear after 3 minutes,
> check for pending InstallPlans and approve them (see Step 1).

## Variables

Set these once and use them throughout:

```bash
CTX="<your-kube-context>"
CLUSTER_DOMAIN=$(oc get ingress.config cluster --context="$CTX" -o jsonpath='{.spec.domain}')
```

---

## Step 1: Install the RHBK Operator

The Red Hat Build of Keycloak (RHBK) operator **only supports OwnNamespace
install mode**. It must be installed into the same namespace where Keycloak
instances will run, with a dedicated OperatorGroup. AllNamespaces mode will
fail silently.

Create the namespace and operator resources:

```bash
oc apply --context="$CTX" -f rhbk-subscription.yaml
```

Wait for the CSV to succeed:

```bash
oc get csv -n keycloak --context="$CTX" -w
```

You should see `rhbk-operator.v24.*` reach `Succeeded`. If the InstallPlan
is pending approval:

```bash
PLAN=$(oc get installplan -n keycloak --context="$CTX" \
  -o jsonpath='{.items[?(@.spec.approved==false)].metadata.name}')
oc patch installplan "$PLAN" -n keycloak --context="$CTX" \
  --type=merge -p '{"spec":{"approved":true}}'
```

## Step 2: Deploy PostgreSQL for Keycloak

Keycloak needs a database backend. Deploy a minimal PostgreSQL StatefulSet:

```bash
oc apply --context="$CTX" -f keycloak-postgresql.yaml
```

Wait for the pod to be ready:

```bash
oc get pods -n keycloak --context="$CTX" -l app=postgresql-db -w
```

## Step 3: Deploy the Keycloak CR

Apply the Keycloak custom resource. Replace `<CLUSTER_DOMAIN>` with your
cluster's apps domain first, or use the provided sed command:

```bash
sed "s/<CLUSTER_DOMAIN>/${CLUSTER_DOMAIN}/g" keycloak-cr.yaml \
  | oc apply --context="$CTX" -f -
```

## Step 4: Expose Keycloak via Route

```bash
oc apply --context="$CTX" -f keycloak-route.yaml
```

## Step 5: Wait for Keycloak Ready

```bash
oc wait keycloak/keycloak -n keycloak --context="$CTX" \
  --for=condition=Ready --timeout=300s
```

## Step 6: Get Admin Credentials

The operator creates a `keycloak-initial-admin` Secret automatically:

```bash
ADMIN_USER=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" \
  -o jsonpath='{.data.username}' | base64 -d)
ADMIN_PASS=$(oc get secret keycloak-initial-admin -n keycloak --context="$CTX" \
  -o jsonpath='{.data.password}' | base64 -d)

echo "Admin user: ${ADMIN_USER}"
echo "Admin pass: ${ADMIN_PASS}"
```

Set the Keycloak URL:

```bash
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" \
  -o jsonpath='{.spec.host}')"
echo "Keycloak URL: ${KEYCLOAK_URL}"
```

Verify access:

```bash
curl -sk "${KEYCLOAK_URL}/realms/master" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['realm'])"
# Expected: master
```

## Step 7: Create the mcp-gateway Realm

Run the setup script. It creates:
- The `mcp-gateway` realm
- A `mcp-gateway` client (service account enabled)
- Groups: `mcp-admins`, `mcp-users`, `mcp-github`
- A `groups` client scope with a group-membership mapper
- Assigns the `groups` scope to the `mcp-gateway` client
- Puts the `mcp-gateway` service account into `mcp-admins`

```bash
export CTX KEYCLOAK_URL
bash setup-keycloak-realm.sh
```

The script is idempotent -- running it twice will not create duplicates.

**Critical:** When requesting tokens, you **must** include `scope=openid groups`
or the `groups` claim will be absent from the JWT. Without it, the AuthPolicy
Rego policy sees no groups and defaults to user-level tools. This fails
silently -- you get fewer tools with no error message.

## Step 8: Generate Wristband Signing Keys

The wristband mechanism works as follows:
1. Authorino validates the Keycloak JWT
2. OPA Rego determines the allowed tools based on the user's groups
3. Authorino signs a short-lived wristband JWT containing `allowed-tools`
4. The broker reads the wristband and filters the tool list

Generate an ECDSA P-256 key pair and create the secrets:

```bash
export CTX
bash generate-wristband-keys.sh
```

This creates:
- `wristband-signing-key` in `kuadrant-system` (private key, used by Authorino)
- `wristband-public-key` in `mcp-system` (public key, used by the MCP broker)
- Patches the MCPGatewayExtension with `trustedHeadersKey`

Verify the patch:

```bash
oc get mcpgatewayextension mcp-gateway -n mcp-system --context="$CTX" \
  -o jsonpath='{.spec.trustedHeadersKey}'
# Expected: {"generate":"Disabled","secretName":"wristband-public-key"}
```

## Step 9: Apply the AuthPolicy

The AuthPolicy configures:
- JWT authentication against the Keycloak issuer
- OPA Rego authorization mapping groups to tool lists
- Wristband token issuance with the `allowed-tools` claim
- Authorization header stripping (see known issue below)

Replace the Keycloak issuer URL and apply:

```bash
KEYCLOAK_ISSUER="${KEYCLOAK_URL}/realms/mcp-gateway"

sed "s|KEYCLOAK_ISSUER|${KEYCLOAK_ISSUER}|g" authpolicy.yaml \
  | oc apply --context="$CTX" -f -
```

Wait for the policy to be accepted:

```bash
oc get authpolicy mcp-gateway-auth -n mcp-system --context="$CTX" \
  -o jsonpath='{.status.conditions}' | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(f\"{c['type']}: {c['status']}\")
"
```

!!! important "Authorization Header Forwarding"

    By default, the gateway forwards all request headers to backend MCP
    servers, including the `Authorization` header. The OpenShift MCP server
    interprets this as a Kubernetes API token, which fails because the
    Keycloak JWT is not valid for the K8s API. The AuthPolicy in this
    module strips the `Authorization` header before forwarding, ensuring
    the MCP server uses its ServiceAccount token instead.

## Step 10: Test Authenticated Access

Get a token as the `mcp-gateway` client (admin group):

```bash
ADMIN_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" \
  -d "username=${ADMIN_USER}" \
  -d "password=${ADMIN_PASS}" \
  -d "grant_type=password" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

CLIENT_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

echo "Client secret: ${CLIENT_SECRET}"
```

Request a token (note `scope=openid groups`):

```bash
TOKEN=$(curl -sk -X POST \
  "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-gateway" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "grant_type=client_credentials" \
  -d "scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

Verify the gateway responds to an authenticated `initialize` request:

> **Note:** The MCP streamable-http protocol requires an `initialize`
> call before `tools/list`. For a quick verification, use `initialize`
> which returns server capabilities.

```bash
oc exec -n mcp-system deploy/mcp-gateway --context="$CTX" -- \
  curl -s http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp \
  -H "Host: openshift.mcp.${CLUSTER_DOMAIN}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}},"id":1}' \
  | python3 -m json.tool
```

**Expected:** A response with `serverInfo` from "Kuadrant MCP Gateway" and `capabilities` including `tools`.

Test unauthenticated access:

```bash
oc exec -n mcp-system deploy/mcp-gateway --context="$CTX" -- \
  curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  http://mcp-gateway-data-science-gateway-class.mcp-system.svc.cluster.local:8080/mcp \
  -H "Host: openshift.mcp.${CLUSTER_DOMAIN}" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

**Expected:** `HTTP 401`

## Step 11: Extend Token Lifetime for the Workshop

Default Keycloak access token lifetime is 5 minutes. For a workshop, set it
to 1 hour so students don't have to keep refreshing:

```bash
ADMIN_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" \
  -d "username=${ADMIN_USER}" \
  -d "password=${ADMIN_PASS}" \
  -d "grant_type=password" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk -X PUT \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway" \
  -d '{"accessTokenLifespan": 3600}'
```

Verify:

```bash
curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway" \
  | python3 -c "import sys,json; print(f'Token lifetime: {json.load(sys.stdin)[\"accessTokenLifespan\"]}s')"
# Expected: Token lifetime: 3600s
```

---

## What You Built

```
Keycloak (mcp-gateway realm)
  |
  |  JWT with groups claim
  v
MCP Gateway (AuthPolicy)
  |
  |  1. Validate JWT (Authorino)
  |  2. OPA Rego: groups -> allowed-tools
  |  3. Sign wristband with allowed-tools claim
  |  4. Strip Authorization header
  |
  v
Broker -> MCP Server (uses ServiceAccount token, not user JWT)
```

The OPA Rego policy in the AuthPolicy maps:
- `mcp-admins` group -> 14 tools (full cluster access)
- All other authenticated users -> 8 tools (read-only)

## What You Deployed

- **RHBK Operator + Keycloak instance** in `keycloak` namespace with PostgreSQL backend
- **mcp-gateway realm** with `mcp-admins` and `mcp-users` groups, a `groups` client scope, and a service account client
- **ECDSA wristband signing keys** -- private key in `kuadrant-system`, public key in `mcp-system`
- **AuthPolicy** on the MCP Gateway -- JWT validation, OPA Rego group-to-tool mapping, wristband issuance, and Authorization header stripping

---

**Next**: [Module 11 -- Deploy the Agent Stack](../11-deploy-agent/README.md)

---

## Troubleshooting

**Empty tool list (0 tools):**
Check that `scope=openid groups` is in the token request. Decode the JWT
and verify the `groups` claim exists:

```bash
echo "$TOKEN" | cut -d. -f2 | python3 -c "
import sys, base64, json
p = sys.stdin.read().strip()
p += '=' * (4 - len(p) % 4)
print(json.dumps(json.loads(base64.urlsafe_b64decode(p)), indent=2))
"
```

**401 on all requests:**
Verify the AuthPolicy `issuerUrl` matches your Keycloak URL exactly. Check
the Authorino pod logs:

```bash
oc logs -l authorino-resource=authorino -n kuadrant-system --context="$CTX" --tail=50
```

**Backend MCP server returns "provide credentials":**
The `Authorization` header stripping in the AuthPolicy is not working. Check
that the `authorization` response header is set to empty string in the policy.
