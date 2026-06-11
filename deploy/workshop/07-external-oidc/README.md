# Module 7: External OIDC

Configure OpenShift to use Keycloak as its external OIDC identity provider.
After this module, Keycloak JWTs are valid Kubernetes API tokens — the same
token a developer uses at the MCP Gateway also authenticates their K8s API
calls. This is the lynchpin of per-user identity at the OpenShift MCP
server: in Module 8, the gateway passes the user's JWT through to the
server, which uses it for K8s API calls under the *user's* RBAC, with the
*user's* name in the audit log.

**Time:** 20--30 minutes (plus 10--15 minutes of kube-apiserver rollout)

**Prerequisites:**
- Module 6 complete (Keycloak realm with `console-oidc` and `oc-cli` clients)
- Cluster-admin access via a method that survives this change (see warning)

> **Working directory:**
>
> ```bash
> cd deploy/workshop/07-external-oidc
> ```

---

!!! danger "Read this before applying anything"

    Setting `type: OIDC` on the Authentication CR **removes the built-in
    OAuth server**. Every cached `oc login` token (including kubeadmin's)
    becomes invalid the moment the rollout completes. Before Step 2:

    1. Create a long-lived ServiceAccount token you control:

       ```bash
       oc create token deployer -n kube-system --duration=24h > /tmp/break-glass-token 2>/dev/null \
         || oc -n kube-system create sa break-glass \
         && oc adm policy add-cluster-role-to-user cluster-admin -z break-glass -n kube-system \
         && oc create token break-glass -n kube-system --duration=24h > /tmp/break-glass-token
       ```

    2. Verify it works **before** proceeding:

       ```bash
       oc whoami --token="$(cat /tmp/break-glass-token)"
       ```

    3. If you have bastion access to a `system:admin` kubeconfig, confirm
       it too. The kubeadmin *password* still works for break-glass only if
       you reconfigure back to integrated OAuth.

    This is why this module sits early in the workshop — do it before
    students accumulate session state, not at the end.

## What Happens to Console Login (and Your Alternatives)

After this module, the OpenShift console redirects to Keycloak. This is a
**hard consequence, not a side effect**: the Authentication CR's `type` is
exclusive — `IntegratedOAuth` (the classic login screen, kubeadmin,
htpasswd, IdP buttons) or `OIDC` (external). Setting `type: OIDC` removes
the built-in OAuth server entirely; the `oauth-openshift` route stops
existing, so there is no classic login screen to fall back to. There is no
side-by-side mode.

What you can do about each thing you might miss:

- **Admin console access** — a Keycloak user in a group mapped to
  `cluster-admin` (this workshop's `developer-a`). For real deployments,
  create a dedicated platform-admin user/group in Keycloak.
- **Other login methods (corporate SSO, LDAP, GitHub, …)** — federate them
  *into Keycloak* as identity providers. Keycloak's login page can carry
  the same IdP buttons the classic OpenShift page did; for most
  enterprises, "console redirects to corporate SSO" is the desired end
  state (an Entra ID deployment redirects to Microsoft login).
- **CLI access** — `oc login` works via the `oc-cli` public client (OIDC
  exec plugin); automation uses ServiceAccount tokens, which are unaffected.
- **Break-glass** — SA tokens and certificate kubeconfigs keep working
  (that's the warning box above).

**The alternative, and its cost:** revert to `IntegratedOAuth` and add
Keycloak as an OpenID *identity provider* on the OAuth CR. You keep the
classic login screen (kubeadmin + a Keycloak button), and the MCP Gateway
itself is unaffected — tool authorization, filtering, and the Vault/GitHub
per-user path (Module 11) all keep working, because clients get their JWTs
from Keycloak directly. But console logins then yield OpenShift OAuth
tokens (`sha256~...`), not JWTs, and **the K8s API no longer accepts
Keycloak JWTs** — so the per-user K8s identity chain (passthrough policy,
per-user RBAC, audit attribution in Module 9) breaks. The OpenShift MCP
server falls back to the shared-SA variant
(`../08-authpolicies/authpolicy-openshift-route-sa.yaml`), which exists
for exactly this configuration. Token exchange does not rescue it: with
integrated OAuth there is no K8s-acceptable token Keycloak can mint.

**It is reversible.** Flipping back to `IntegratedOAuth` (another
10--15-minute kube-apiserver rollout) restores the classic login and the
kubeadmin password. A demo cluster can run External OIDC during the
workshop and be reverted afterwards.

## Variables

```bash
CTX="<your-kube-context>"
CLUSTER_DOMAIN=$(oc get ingress.config cluster --context="$CTX" -o jsonpath='{.spec.domain}')
KEYCLOAK_URL="https://$(oc get route keycloak -n keycloak --context="$CTX" -o jsonpath='{.spec.host}')"
KEYCLOAK_ISSUER="${KEYCLOAK_URL}/realms/mcp-gateway"
# ADMIN_TOKEN from Module 6 Step 9, or re-acquire via admin-cli
```

## Step 1: Create the Console Client Secret

Retrieve the `console-oidc` client secret from Keycloak (created by the
Module 6 realm setup script) and store it in `openshift-config`:

```bash
CONSOLE_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=console-oidc" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

CONSOLE_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${CONSOLE_UUID}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

oc create secret generic console-oidc-secret \
  -n openshift-config --context="$CTX" \
  --from-literal=clientSecret="${CONSOLE_SECRET}"
```

## Step 2: Patch the Authentication CR

The Authentication CR created by the installer carries a
`webhookTokenAuthenticator` field from the integrated OAuth setup. That
field **cannot coexist with `type: OIDC`**, and `oc apply` performs a
merge that leaves it in place — applying directly fails with
`spec.webhookTokenAuthenticator: Invalid value: ... this field cannot be
set with the "OIDC" .spec.type`. Remove it first, then apply:

```bash
oc patch authentication.config.openshift.io cluster --context="$CTX" \
  --type=json -p '[{"op":"remove","path":"/spec/webhookTokenAuthenticator"}]' \
  2>/dev/null || true   # already absent on some clusters

sed -e "s|KEYCLOAK_ISSUER|${KEYCLOAK_ISSUER}|g" \
    -e "s|CONSOLE_OIDC_SECRET|console-oidc-secret|g" \
    authentication-cr.yaml \
  | oc apply --context="$CTX" -f -
```

Wait for the `kube-apiserver` cluster operator to stabilize (10--15
minutes as it rolls across all control plane nodes):

```bash
oc get co kube-apiserver authentication --context="$CTX" -w
```

Both should show `AVAILABLE=True`, `PROGRESSING=False`, `DEGRADED=False`.

## Step 3: Map Keycloak Groups to Kubernetes RBAC

This is where per-user authorization gets its teeth. Map the Keycloak
groups to ClusterRoles:

```bash
oc adm policy add-cluster-role-to-group cluster-admin mcp-admins --context="$CTX"
oc adm policy add-cluster-role-to-group view mcp-users --context="$CTX"
```

Now `developer-a` (mcp-admins) is cluster-admin and `developer-b`
(mcp-users) is view-only — *as themselves*, with their own usernames in
the K8s audit log.

> Adjust these mappings for production: `cluster-admin` is for workshop
> clarity. The point is that backend authorization is now ordinary K8s
> RBAC, managed per group/user like any other cluster access.

## Step 4: Verify

Test that a Keycloak JWT works as a K8s API token (use `CLIENT_SECRET`
from Module 6 Step 10):

```bash
DEV_A_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-gateway&client_secret=${CLIENT_SECRET}&grant_type=password&username=developer-a&password=developer-a&scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

oc whoami --token="$DEV_A_TOKEN" --server="https://api.${CLUSTER_DOMAIN#apps.}:6443"
# Expected: developer-a
```

Verify RBAC differs per user:

```bash
DEV_B_TOKEN=$(curl -sk -X POST "${KEYCLOAK_URL}/realms/mcp-gateway/protocol/openid-connect/token" \
  -d "client_id=mcp-gateway&client_secret=${CLIENT_SECRET}&grant_type=password&username=developer-b&password=developer-b&scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

oc auth can-i create configmaps -n mcp-ecosystem --token="$DEV_A_TOKEN" --server="https://api.${CLUSTER_DOMAIN#apps.}:6443"
# Expected: yes
oc auth can-i create configmaps -n mcp-ecosystem --token="$DEV_B_TOKEN" --server="https://api.${CLUSTER_DOMAIN#apps.}:6443"
# Expected: no
```

Open the console URL in a browser — it should redirect to Keycloak for
login. Log in as `developer-a`.

---

## What You Built

| Resource | Purpose |
|---|---|
| console-oidc-secret (openshift-config) | Console OIDC client credential |
| Authentication CR (`type: OIDC`) | Keycloak JWTs are valid K8s API tokens |
| ClusterRoleBindings (groups) | mcp-admins → cluster-admin, mcp-users → view |

The identity chain is now: one Keycloak login → one JWT → valid at the MCP
Gateway (Module 8) **and** at the K8s API — so when the gateway passes the
JWT through to the OpenShift MCP server, every K8s call is made as the
actual developer.

---

**Next**: [Module 8 -- AuthPolicies](../08-authpolicies/README.md)
