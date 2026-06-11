# Module 6: Identity — Keycloak

Install Keycloak and build the identity foundation for the MCP Gateway:
the realm, groups, workshop users, per-tool client roles, and the wristband
signing keys. This module creates *identities and permissions*; turning
them into enforcement happens in Module 7 (External OIDC) and Module 8
(AuthPolicies).

After this module you will have:

- A `mcp-gateway` realm with groups (`mcp-admins`, `mcp-users`) and users
  (`developer-a`, `developer-b`)
- Bearer-only Keycloak clients matching the MCPServerRegistration names,
  whose client roles represent individual tools
- Wristband signing keys wired into the MCPGatewayExtension

**Time:** 30--45 minutes

**Prerequisites:**
- Modules 1--5 complete (gateway infrastructure, MCP Gateway, MCP server,
  registration)
- `openssl` available on your workstation

> **Working directory:**
>
> ```bash
> cd deploy/workshop/06-identity-keycloak
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
- A `mcp-gateway` client (service account enabled, direct access grants) with an audience mapper
- A `console-oidc` client (confidential, for OpenShift console OIDC login)
- An `oc-cli` client (public, for OpenShift CLI OIDC login)
- Groups: `mcp-admins`, `mcp-users`, `mcp-github`
- A `groups` client scope with a group-membership mapper
- Bearer-only clients matching MCPServerRegistration names (`mcp-ecosystem/openshift-mcp-server`, `mcp-ecosystem/github-mcp-server`) with client roles for each tool — including the write tool `pods_run`
- Workshop users `developer-a` (admin -- all tool roles, including the write tool `pods_run`) and `developer-b` (user -- read-only subset)
- Assigns `groups` scope to the `mcp-gateway`, `console-oidc`, and `oc-cli` clients
- Assigns built-in `roles` scope to the `mcp-gateway` client
- Puts the `mcp-gateway` service account into `mcp-admins` **and assigns it all tool roles** — the agent (Module 16) authenticates as this service account, and an identity without tool roles gets a zero-tool list from the broker

```bash
export CTX KEYCLOAK_URL CLUSTER_DOMAIN
bash setup-keycloak-realm.sh
```

The script is idempotent -- running it twice will not create duplicates.

> **Tool roles must match the server's real tool list.** The script's role
> list matches the current server image. If a future image changes the
> tool set, run `tools/list` (Module 5 Step 4) and create/remove roles to
> match — tool names that exist as roles but not as tools are harmless;
> tools without roles are uncallable by everyone.

**Critical:** When requesting tokens, you **must** include `scope=openid groups`
or the `groups` claim will be absent from the JWT. Without it, VirtualMCPServer
routing fails silently -- users see an empty tool list. The `resource_access`
claim (used by the Rego for tool enforcement) is included by default via the
built-in `roles` scope and does not need to be explicitly requested.

## Step 8: Generate Wristband Signing Keys

The wristband mechanism (activated by the AuthPolicy in Module 8) works as
follows:
1. Authorino validates the Keycloak JWT
2. OPA Rego determines the allowed tools based on the user's client roles
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

## Step 9: Extend Token Lifetime for the Workshop

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

## Step 10: Verify a Token Carries the Right Claims

Get the `mcp-gateway` client secret:

```bash
CLIENT_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=mcp-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

CLIENT_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CLIENT_UUID}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

echo "Client secret: ${CLIENT_SECRET}"
```

Request a token as developer-a (note `scope=openid groups`) and decode it:

```bash
TOKEN=$(curl -sk -X POST \
  "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-gateway" -d "client_secret=${CLIENT_SECRET}" \
  -d "grant_type=password" -d "username=developer-a" -d "password=developer-a" \
  -d "scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "$TOKEN" | cut -d. -f2 | python3 -c "
import sys, base64, json
p = sys.stdin.read().strip()
p += '=' * (4 - len(p) % 4)
c = json.loads(base64.urlsafe_b64decode(p))
print('groups:', c.get('groups'))
print('resource_access:', json.dumps(c.get('resource_access'), indent=2))
"
```

**Expected:** `groups` contains `mcp-admins`, and `resource_access` contains
`mcp-ecosystem/openshift-mcp-server` with the full tool role list, including
`pods_run`.

> The gateway does not require these tokens *yet* — enforcement is wired in
> Module 8. Per-user MCP identity is no longer a future enhancement: it is
> completed across Modules 7--9 (and Module 11 for GitHub).

---

## What You Built

```
Keycloak (mcp-gateway realm)
  ├── Groups: mcp-admins, mcp-users        → VirtualMCPServer routing (Module 8)
  ├── Users: developer-a, developer-b      → per-user identity end to end
  ├── Bearer-only clients per MCP server   → client roles = tool permissions
  │     mcp-ecosystem/openshift-mcp-server (15 roles, incl. write tool pods_run)
  │     mcp-ecosystem/github-mcp-server
  └── Wristband ECDSA keys                 → tools/list filtering (Module 8)
```

Tool permissions are managed in Keycloak as client roles on bearer-only
clients that match MCPServerRegistration names. The AuthPolicies in Module 8
read these from the JWT's `resource_access` claim dynamically -- adding or
removing tool permissions only requires Keycloak changes, not policy edits.

---

## Additional Materials

### Keycloak Client Roles Primer

This module uses Keycloak **client roles** to manage per-tool permissions. Here is how the pieces fit together.

**Bearer-only clients as role containers.** Each MCP server has a matching Keycloak client whose only purpose is to hold client roles. The client ID matches the MCPServerRegistration name exactly (e.g., `mcp-ecosystem/openshift-mcp-server` — Keycloak supports `/` in client IDs). Bearer-only clients cannot be used for login -- they only provide a namespace for roles.

**Client roles represent tools.** Each tool exposed by an MCP server is a client role on that server's Keycloak client. Role names match the tool names exactly (e.g., `pods_list`). In MCP Gateway v0.7.0 tools are unprefixed unless names collide across servers, so role names and tool names align one-to-one.

**`resource_access` JWT claim.** When a user has client roles assigned, Keycloak includes them in the JWT under `resource_access.<client-id>.roles`:

```json
{
  "resource_access": {
    "mcp-ecosystem/openshift-mcp-server": {
      "roles": ["pods_list", "pods_get", "namespaces_list", "pods_run"]
    },
    "mcp-ecosystem/github-mcp-server": {
      "roles": ["search_code", "get_file_contents"]
    }
  }
}
```

The Module 8 Rego reads this claim directly: `input.auth.identity.resource_access[servername].roles`. No hardcoded tool lists are needed in the AuthPolicy.

**Managing permissions at scale.** To grant or revoke tool access:
1. Open the Keycloak Admin Console
2. Navigate to the MCP server's bearer-only client
3. Go to the **Roles** tab to see available tool roles
4. Navigate to **Users** > select a user > **Role Mappings**
5. Assign or remove client roles from the MCP server client

Alternatively, use the Keycloak Admin REST API:

```bash
# Assign roles to a user
POST /admin/realms/{realm}/users/{user-id}/role-mappings/clients/{client-uuid}
Body: [{"id": "<role-uuid>", "name": "pods_list"}, ...]

# Remove roles from a user
DELETE /admin/realms/{realm}/users/{user-id}/role-mappings/clients/{client-uuid}
Body: [{"id": "<role-uuid>", "name": "pods_list"}, ...]
```

No AuthPolicy changes are needed -- the Rego reads permissions from the JWT at request time.

**Client roles vs realm roles.** Realm roles apply globally across all clients. Client roles are scoped to a specific client. For tool-level authorization, client roles are the right choice because each MCP server has its own set of tools.

**Client roles vs groups.** Both are used for different purposes:
- **Groups** (`mcp-admins`, `mcp-users`) control VirtualMCPServer routing -- which tool *view* a user sees in `tools/list`
- **Client roles** control tool *authorization* -- which tools a user can actually call via `tools/call`

---

**Next**: [Module 7 -- External OIDC](../07-external-oidc/README.md)
