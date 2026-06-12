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
- Module 6 complete (Keycloak realm with `console-oidc`, `oc-cli`, and `rhoai-gateway` clients)
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
       oc -n kube-system create sa break-glass
       oc adm policy add-cluster-role-to-user cluster-admin -z break-glass -n kube-system
       oc create token break-glass -n kube-system --duration=24h > /tmp/break-glass-token
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
- **CLI access** — `oc login` works via the `oc-cli` public client and the
  OIDC exec plugin:

  ```bash
  oc login https://api.<cluster>:6443 \
    --issuer-url ${KEYCLOAK_ISSUER} \
    --exec-plugin oc-oidc --client-id oc-cli
  ```

  The plugin opens a browser and listens on a **random localhost port** for
  the OAuth callback — which is why the `oc-cli` Keycloak client uses
  wildcard redirect URIs (`http://localhost*`, `http://127.0.0.1*`). An
  exact redirect URI causes Keycloak to reject the login with
  `Invalid parameter: redirect_uri`. Automation should keep using
  ServiceAccount tokens, which are unaffected.
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
`webhookTokenAuthenticator` field from the integrated OAuth setup, which
cannot coexist with `type: OIDC`. The change must be made **in a single
atomic request**: the authentication operator actively reconciles the
webhook field back, so removing it first and applying OIDC second is a
race you will usually lose ("this field cannot be set with the \"OIDC\"
.spec.type"). A JSON *merge patch* clears the field (`null`) and sets the
OIDC config in one operation:

```bash
oc patch authentication.config.openshift.io cluster --context="$CTX" \
  --type=merge \
  -p "$(sed -e "s|KEYCLOAK_ISSUER|${KEYCLOAK_ISSUER}|g" \
            -e "s|CONSOLE_OIDC_SECRET|console-oidc-secret|g" \
            authentication-oidc-patch.json)"
```

(`authentication-cr.yaml` in this directory shows the same configuration
as a full CR, for reference.)

**Mid-rollout, your current `oc` session WILL start returning
`Unauthorized`** — that is the old kube:admin OAuth token dying, on
schedule. Switch to the break-glass token and continue:

```bash
oc config set-credentials break-glass --token="$(cat /tmp/break-glass-token)"
oc config set-context "$CTX" --user=break-glass
oc whoami --context="$CTX"   # system:serviceaccount:kube-system:break-glass
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

> The commands print `Warning: Group 'mcp-admins' not found` — this is
> expected and harmless. The groups exist in Keycloak JWTs, not as
> OpenShift Group objects; the binding applies to whatever presents the
> group claim.

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

## Step 5: Reconnect the OpenShift AI Dashboard

The RHOAI 3.x dashboard is served behind the **Data Science Gateway**
(`https://rh-ai.<CLUSTER_DOMAIN>`), whose auth layer — `kube-auth-proxy`,
an oauth2-proxy derivative running in `openshift-ingress` — authenticates
against the integrated OAuth server by default. After this module that
server no longer exists, and the gateway does not inherit the cluster's
OIDC config: its `GatewayConfig` goes `Ready=False` with
`Cluster is in OIDC mode but GatewayConfig has no OIDC configuration`,
and the dashboard is unreachable. It needs the same treatment the console
got in Step 1 — its own OIDC client, wired explicitly.

Check the symptom first:

```bash
oc get gatewayconfig default-gateway --context="$CTX"
# READY=False, REASON=NotReady until configured
```

Retrieve the `rhoai-gateway` client secret (created by the Module 6 realm
script) and store it where the GatewayConfig expects:

```bash
RHOAI_UUID=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients?clientId=rhoai-gateway" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

RHOAI_SECRET=$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  "${KEYCLOAK_URL}/admin/realms/mcp-gateway/clients/${RHOAI_UUID}/client-secret" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])")

oc create secret generic rhoai-oidc-client-secret \
  -n openshift-ingress --context="$CTX" \
  --from-literal=clientSecret="${RHOAI_SECRET}"
```

Patch the GatewayConfig. Note `cookie.refresh: 30m` — the gateway requires
it to be **less than** the OIDC provider's access-token lifespan, and the
default (1h) exactly equals this workshop's Keycloak setting:

```bash
oc patch gatewayconfig default-gateway --context="$CTX" --type=merge -p "{
  \"spec\": {
    \"cookie\": {\"refresh\": \"30m0s\"},
    \"oidc\": {
      \"clientID\": \"rhoai-gateway\",
      \"issuerURL\": \"${KEYCLOAK_ISSUER}\",
      \"clientSecretRef\": {\"name\": \"rhoai-oidc-client-secret\", \"key\": \"clientSecret\"},
      \"secretNamespace\": \"openshift-ingress\"
    }
  }
}"
```

> **The create-once credentials trap.** `GatewayConfig` will report
> `Ready=True` and the dashboard will redirect to Keycloak — **with the
> wrong client ID**. The operator renders `spec.oidc` into the
> `kube-auth-proxy-creds` Secret only at creation time (it also holds the
> session-cookie secret, so updates would invalidate every session) and
> never reconciles it afterwards. Since RHOAI was installed before this
> module, that Secret already exists with an auto-generated
> `data-science` client ID. Delete it so the operator recreates it from
> `spec.oidc`, then restart the proxy:
>
> ```bash
> oc delete secret kube-auth-proxy-creds -n openshift-ingress --context="$CTX"
> # wait for the operator to recreate it (~30s), then:
> oc get secret kube-auth-proxy-creds -n openshift-ingress --context="$CTX" \
>   -o jsonpath='{.data.OAUTH2_PROXY_CLIENT_ID}' | base64 -d   # rhoai-gateway
> oc rollout restart deployment/kube-auth-proxy -n openshift-ingress --context="$CTX"
> ```

Verify the redirect carries the right client, then log in as
`developer-a` at `https://rh-ai.${CLUSTER_DOMAIN}`:

```bash
curl -sk -o /dev/null -w '%{redirect_url}\n' "https://rh-ai.${CLUSTER_DOMAIN}/"
# expect a Keycloak /auth URL containing client_id=rhoai-gateway
```

!!! warning "Two failure modes you may still hit"

    - **HTTP 403 after the Keycloak login** — kube-auth-proxy logs
      `email in id_token (...) isn't verified`. oauth2-proxy hard-rejects
      unverified emails. The Module 6 realm script sets
      `emailVerified: true` on the workshop users; if your users predate
      that fix, update them via the Keycloak admin API or console.
    - **"Unauthorized" after a successful login** — the dashboard is
      calling the K8s API with your `aud: rhoai-gateway` token, but the
      Authentication CR doesn't accept that audience. This module's
      patch already includes `rhoai-gateway` in `audiences`; if you
      added it after the fact, expect another 10–15 minute
      kube-apiserver rollout before logins work.

---

## What You Built

| Resource | Purpose |
|---|---|
| console-oidc-secret (openshift-config) | Console OIDC client credential |
| Authentication CR (`type: OIDC`) | Keycloak JWTs are valid K8s API tokens |
| ClusterRoleBindings (groups) | mcp-admins → cluster-admin, mcp-users → view |
| rhoai-oidc-client-secret (openshift-ingress) | RHOAI Data Science Gateway OIDC client credential |
| GatewayConfig `spec.oidc` | OpenShift AI dashboard login via Keycloak |

The identity chain is now: one Keycloak login → one JWT → valid at the MCP
Gateway (Module 8) **and** at the K8s API — so when the gateway passes the
JWT through to the OpenShift MCP server, every K8s call is made as the
actual developer.

---

**Next**: [Module 8 -- AuthPolicies](../08-authpolicies/README.md)
