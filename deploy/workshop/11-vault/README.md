# Module 11: Vault — Per-User GitHub Credentials

Complete the per-user identity story for the GitHub MCP server. Module 10
deployed it with a single shared PAT — every GitHub API call looked like
the same account. This module stores **each developer's own GitHub PAT in
Vault** and has the gateway exchange the user's Keycloak JWT for *their*
PAT at request time, per request, transparently to both the user and the
server.

The flow (the per-route AuthPolicy does all of it):

1. Validate the user's Keycloak JWT (forwarded by the broker on the
   hairpin leg — this is why the client-plane policy never strips it)
2. `vault-login`: exchange the JWT for a Vault client token (cached per
   user for 5 minutes)
3. `vault`: read the user's PAT from the KV store, path keyed by
   `preferred_username`
4. Replace the `Authorization` header with `Bearer <their-PAT>`

The GitHub MCP server (v1.2.0+, HTTP mode) builds a per-request GitHub
client from the incoming Bearer token, so every GitHub API call is made
**as the actual developer**.

**Time:** 45--60 minutes

**Prerequisites:**
- Modules 1--10 complete (gateway, identity, AuthPolicies, GitHub server)
- `helm` CLI installed
- One GitHub PAT per workshop user — see the PAT requirements below

!!! important "PAT requirements (read this before creating tokens)"

    To actually *demonstrate* per-user identity, the two PATs must be
    distinguishable:

    - **Fine-grained PATs** (not classic), ideally from **two different
      GitHub accounts**; if you must use one account, scope the repository
      access differently per token.
    - **Repository access**: developer-a gets all target repos;
      developer-b gets a restricted subset.
    - **Permissions → Repository permissions → Contents: Read and write.**
      Read-only PATs against **public** repos are indistinguishable —
      GitHub grants every fine-grained PAT read access to public repos
      regardless of repository selection. The per-user difference is only
      visible on writes (or on private repos).
    - For a read-only demo, at least one target repo must be **private**.

> **Working directory:**
>
> ```bash
> cd deploy/workshop/11-vault
> ```

## Variables

```bash
CTX="<your-kube-context>"
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" -o jsonpath='{.spec.host}')"
KEYCLOAK_ISSUER="${KEYCLOAK_URL}/realms/mcp-gateway"
```

---

## Step 1: Install Vault

For the workshop, run Vault in dev mode (in-memory, auto-unsealed, KV v2
pre-mounted at `secret/`). **Dev mode is not for production** — production
deployments need HA storage, auto-unseal, and TLS.

```bash
helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update hashicorp   # scoped: avoids failures from unrelated stale repos

helm upgrade -i vault hashicorp/vault \
  --namespace vault --create-namespace \
  --kube-context "$CTX" \
  --set server.dev.enabled=true \
  --set server.dev.devRootToken=workshop-root \
  --set injector.enabled=false \
  --set global.openshift=true \
  --set server.image.repository=docker.io/hashicorp/vault \
  --set server.image.tag=1.19
```

!!! warning "Image override is required"

    With `global.openshift=true`, recent chart versions default the server
    image to `registry.connect.redhat.com/hashicorp/vault:<chart-version>`,
    a tag that does not exist — the pod sits in `ImagePullBackOff`. The two
    `server.image.*` overrides above pin a real image on docker.io.

Wait for the pod:

```bash
oc get pods -n vault --context="$CTX" -w
```

All `vault` CLI commands below run inside the pod:

```bash
vexec() { oc exec -n vault vault-0 --context="$CTX" -- sh -c "VAULT_TOKEN=workshop-root $*"; }
vexec vault status
```

## Step 2: Configure JWT Authentication Against Keycloak

Vault must trust JWTs issued by the `mcp-gateway` realm:

```bash
vexec "vault auth enable jwt" || true

vexec "vault write auth/jwt/config \
  jwks_url='${KEYCLOAK_ISSUER}/protocol/openid-connect/certs' \
  bound_issuer='${KEYCLOAK_ISSUER}'"
```

> If Keycloak's route uses a self-signed or private CA, add
> `jwks_ca_pem` with the CA certificate, or Vault will reject the JWKS
> endpoint.

Create the role Authorino will use. `bound_audiences` must match an `aud`
claim present in user access tokens — the Module 6 realm script adds an
audience mapper for the `mcp-gateway` client:

```bash
vexec "vault write auth/jwt/role/authorino - <<EOF
{
  \"role_type\": \"jwt\",
  \"bound_audiences\": [\"mcp-gateway\"],
  \"user_claim\": \"preferred_username\",
  \"claim_mappings\": {\"preferred_username\": \"preferred_username\"},
  \"policies\": [\"mcp-user-secrets\"],
  \"ttl\": \"1h\"
}
EOF"
```

> **Lightweight token note:** some IdPs (Keycloak 26+ defaults) omit `sub`
> from access tokens. This module keys everything off
> `preferred_username`, which the `profile` scope provides. If you switch
> to `sub`, change the role's `user_claim`, the policy template, and the
> AuthPolicy URL expression together.

## Step 3: Create the Templated Policy

Each authenticated user may read **only their own** secrets:

```bash
ACCESSOR=$(vexec "vault auth list -format=json" | python3 -c "import sys,json; print(json.load(sys.stdin)['jwt/']['accessor'])")

vexec "vault policy write mcp-user-secrets - <<EOF
path \"secret/data/mcp-gateway/users/{{identity.entity.aliases.${ACCESSOR}.metadata.preferred_username}}/*\" {
  capabilities = [\"read\"]
}
EOF"
```

## Step 4: Seed Per-User PATs

One secret per developer, key `github_pat`:

```bash
vexec "vault kv put secret/mcp-gateway/users/developer-a/github github_pat='ghp_DEVELOPER_A_TOKEN'"
vexec "vault kv put secret/mcp-gateway/users/developer-b/github github_pat='ghp_DEVELOPER_B_TOKEN'"
```

Verify:

```bash
vexec "vault kv get secret/mcp-gateway/users/developer-a/github"
```

> Provisioning real users' PATs is an admin workflow you own — the
> injection mechanism only requires that a secret exists at the expected
> path when a tool call happens. Users without a stored PAT get a clear
> 403 ("No GitHub credential found for this user") rather than silently
> acting as someone else.

## Step 5: Apply the Per-Route AuthPolicy

This policy atomically replaces the backend-plane default for the GitHub
route only:

```bash
sed "s|KEYCLOAK_ISSUER|${KEYCLOAK_ISSUER}|g" authpolicy-github-route-vault.yaml \
  | oc apply --context="$CTX" -f -

oc get authpolicy github-mcp-route-auth -n mcp-ecosystem --context="$CTX"
# Expected: ACCEPTED=True ENFORCED=True
```

Keep the `credentialRef` on the MCPServerRegistration — the broker still
uses the static PAT for `tools/list` discovery. Discovery credential and
per-request credentials coexist by design.

## Step 6: Verify Per-User GitHub Identity

Call `github_get_me` as each developer (tokens as in Module 9). The result
must be **each developer's own GitHub profile**:

```bash
# As developer-a (session setup as in Module 9 Step 4)
curl -sk -X POST "https://mcp-gateway.${CLUSTER_DOMAIN}/mcp" \
  -H "Authorization: Bearer ${TOKEN_A}" -H "Mcp-Session-Id: ${SID}" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"github_get_me","arguments":{}},"id":1}'
```

Repeat as developer-b — a different GitHub login comes back. Two
developers, one gateway, one GitHub MCP server, two GitHub identities.

With writes now attributable per user, you may also remove `--read-only`
from the Module 10 Deployment if your use case needs GitHub write tools —
gate them with Keycloak tool roles exactly like the OpenShift write tool.

## Troubleshooting

- **403 "No GitHub credential found"** — no secret at
  `secret/mcp-gateway/users/<username>/github`, or the key isn't
  `github_pat`.
- **403 with vault-login errors in Authorino logs** — audience mismatch
  (decode the token and compare `aud` with the role's `bound_audiences`),
  expired token, or Vault unreachable from the Authorino pod.
- **Wrong identity returned** — the secret path resolved to a different
  user; check the `preferred_username` claim and the policy template.

```bash
oc logs -l authorino-resource=authorino -n kuadrant-system --context="$CTX" --tail=50
```

---

## What You Deployed

| Resource | Namespace | Purpose |
|---|---|---|
| Vault (dev mode) | vault | Per-user secret storage, JWT auth |
| JWT auth role `authorino` | vault | Accepts user Keycloak JWTs |
| Policy `mcp-user-secrets` | vault | Users read only their own paths |
| AuthPolicy github-mcp-route-auth | mcp-ecosystem | Per-request PAT injection |

---

**Next**: [Module 12 -- GPU Node (optional model track)](../12-gpu-node/README.md)
