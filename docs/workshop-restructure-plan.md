# Workshop Restructure Plan

Date: 2026-06-10. Driver: run the full workshop end-to-end on a new cluster,
sequenced so each layer is built correctly the first time and the customer
reaches the stated goal — many developers using Claude Code (or any MCP
client) against one central MCP Gateway, acting as themselves end to end.

Decisions locked in (see `per-user-mcp-identity-fix.md` for background):

1. **Module 17's layered design folds into the core modules** — no
   build-then-fix. The two-plane gateway topology, internal backend
   hostnames, and layered AuthPolicies are built from the start.
2. **External OIDC is promoted to a required core module** placed right
   after Keycloak setup — it is the lynchpin for per-user K8s identity and
   the most disruptive change, so it happens early with break-glass ready.
3. **The OpenShift MCP server gets limited write tools**
   (`read_only = false`, `disable_destructive = true`, Secrets denied) so
   per-user K8s RBAC is demonstrable, not just auditable.
4. **Module 14 (Vault) gets real content** — per-user GitHub PATs complete
   the per-user story for both backends.

## Target Sequence

### Core path (required — no model needed)

| New # | Directory | Content | From |
|---|---|---|---|
| 0 | `00-cluster-prerequisites` | RHOAI, Service Mesh, cert-manager operators | unchanged |
| 1 | `01-gateway-infrastructure` | Kuadrant/RHCL, GatewayClass | was 02 |
| 2 | `02-mcp-gateway` | MCP Gateway operator + **two-plane Gateway** (`mcp` client listener + `mcps` backend listener), broker, **client Route/HTTPRoute** | was 06 + 17 manifests |
| 3 | `03-mcp-server-prerequisites` | SA, RBAC, **write-enabled config.toml** | was 07 |
| 4 | `04-mcp-server` | OpenShift MCP server deployment | was 08 |
| 5 | `05-gateway-registration` | Registration, **`.mcp.local` hostnames**, ReferenceGrant, VirtualMCPServers, **RateLimitPolicy on client listener** | was 09 |
| 6 | `06-identity-keycloak` | Keycloak install, realm, groups, users, tool-role clients, wristband keys | was 10 minus steps 8/10 |
| 7 | `07-external-oidc` | **NEW** — External OIDC + K8s RBAC mapping + break-glass | was 10 step 8 |
| 8 | `08-authpolicies` | **NEW** — layered policies: client plane, backend default, per-route passthrough (SA variant as fallback) | from 17 |
| 9 | `09-developer-onboarding` | **NEW** — Claude Code/MCP Inspector against the gateway URL; per-user tools, RBAC write demo, audit trail. **The goal milestone.** | new |

### Track B — second backend + per-user credentials

| New # | Directory | Content | From |
|---|---|---|---|
| 10 | `10-github-mcp-server` | GitHub MCP server (shared PAT, read-only) | was 08-github-mcp-server |
| 11 | `11-vault` | **NEW CONTENT** — Vault install, JWT auth, templated policy, per-user PAT seeding, per-route injection AuthPolicy | was 14 (stub) |

### Track C — model + agent (optional; requires GPU or external model)

| New # | Directory | From |
|---|---|---|
| 12 | `12-gpu-node` | was 01 |
| 13 | `13-models-as-a-service` | was 03 |
| 14 | `14-deploy-model` | was 04 |
| 15 | `15-model-endpoint` | was 05 |
| 16 | `16-deploy-agent` | was 11 |
| 17 | `17-agent-test` | was 12 |
| 18 | `18-playground` | was 13 |

### Track D — ops + extension (optional)

| New # | Directory | From |
|---|---|---|
| 19 | `19-observability` | was 15 (MinIO decoupled from playground) |
| 20 | `20-add-mcp-server` | was 16 (revised for layered policies) |

`17-per-user-identity` is dissolved: gateway/route manifests → 02, backend
hostname routes → 05 and 10, AuthPolicies → 08, Vault policy → 11. Its
README's analysis lives on in `docs/per-user-mcp-identity-fix.md`.

## Work Items

1. **Directory renames** per the tables above; bulk-fix all cross-reference
   paths (`../NN-name/`) and module-number prose in prerequisites and
   Next/Previous links.
2. **02-mcp-gateway**: replace the Gateway CR with the two-listener version
   (`mcp` = `mcp-gateway.<CLUSTER_DOMAIN>`, `mcps` = `*.mcp.local`, no
   `https` listener); add a step creating the edge-TLS Route + client
   HTTPRoute (→ Istio gateway service, never the broker); "What You
   Deployed" gains the client URL — the single URL used by every consumer
   for the rest of the workshop.
3. **03-mcp-server-prerequisites**: config.toml `read_only = false`,
   `disable_destructive = true`, keep the Secret denial; README explains
   the posture (writes allowed, deletes/destructive blocked, Secrets
   invisible; real authorization comes from Keycloak tool roles + K8s RBAC).
4. **05-gateway-registration**: HTTPRoute hostname →
   `openshift-mcp-server.mcp.local`; VirtualMCPServer lists add
   `openshift_resources_create_or_update` (+ note that tool lists must be
   verified against `tools/list` after deployment); RateLimitPolicy
   retargeted to `sectionName: mcp` so it limits users, not the broker
   hairpin; README explains the two-plane hostname rationale.
5. **06-identity-keycloak**: remove Step 8 (External OIDC) and Step 10
   (AuthPolicy) and the old `authpolicy.yaml`; `setup-keycloak-realm.sh`
   gains the `resources_create_or_update` client role, assigned to
   developer-a only; closing notes updated (per-user identity is no longer
   "a future enhancement").
6. **07-external-oidc** (new): console client secret, Authentication CR
   patch, group→ClusterRole bindings, kube-apiserver rollout wait,
   verification (`oc whoami --token`), break-glass guidance up front.
7. **08-authpolicies** (new): the three policies with KEYCLOAK_ISSUER sed;
   passthrough is the default OpenShift variant (External OIDC done in 07);
   SA-strip variant included as fallback; verification = 401 unauth +
   policy Accepted/Enforced. CRD gotcha documented (`when` + `defaults`
   mutually exclusive).
8. **09-developer-onboarding** (new): token acquisition (password grant;
   device-flow note), Claude Code `mcp add` config and MCP Inspector
   alternative, per-user `tools/list` diff (developer-a vs developer-b),
   write demo (developer-a creates a ConfigMap; developer-b denied by K8s
   RBAC), 403 on out-of-role tool, audit-log attribution.
9. **10-github-mcp-server**: renumber, HTTPRoute hostname →
   `github-mcp-server.mcp.local`, prerequisites corrected (now follows
   registration), note that per-user PATs arrive in Module 11.
10. **11-vault** (new content): Vault Helm install (dev mode for workshop),
    KV v2, JWT auth method against Keycloak JWKS, role `authorino`
    (bound_audiences = client ID), templated per-user policy, PAT seeding
    for developer-a/b, per-route AuthPolicy with two-stage metadata
    pipeline injecting the PAT, verification via `github_get_me` per user.
11. **16/17/18 (agent, agent-test, playground)**: standardize every gateway
    URL on `https://mcp-gateway.<CLUSTER_DOMAIN>/mcp` (the Module 12-era
    multi-level `mcp-gateway.mcp.apps...` example and the in-cluster
    service URLs are replaced).
12. **19-observability**: copy `minio.yaml` into the module; remove the
    dependency on the playground module.
13. **20-add-mcp-server**: HTTPRoute hostname → `.mcp.local`; revise the
    "no AuthPolicy changes" claim (true for credential-less servers via the
    backend default; servers needing backend credentials add a per-route
    policy); renumber.
14. **Top-level docs**: `deploy/workshop/README.md` module table rewritten
    with the four tracks; `CLAUDE.md` stage table updated;
    `docs/per-user-mcp-identity*.md` links updated; delete
    `deploy/workshop/17-per-user-identity/`.

## Verification (post-restructure)

- No file references an old directory name (grep for each old path).
- All YAML parses; AuthPolicies match the forms validated on cluster-n7pd5.
- The core path reads end-to-end with consistent module numbers, one
  client URL, and prerequisites that reference only earlier modules.
