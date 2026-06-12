# Audit: Workshop vs. the MCP Ecosystem Guides

Comparison of this workshop's implementation (modules 0–20, validated
across six live cluster runs, June 2026) against `guides/MCP-Ecosystem/`
and the companion findings file (`mcp-gateway-lessons-learned.md`,
21 numbered items). The guides were validated on OpenShift 4.21.6 /
RHOAI 3.4.0 / **MCP Gateway v0.6.0** / RHCL 1.3.2 (April 2026); the
workshop currently runs MCP Gateway **v0.7.0** / RHCL 1.4.0→1.3.4-pinned,
which explains several deltas.

Verdict in one line: **we are not getting lucky on the auth core — our
deviations there are deliberate, documented, and in two cases more
correct than the guide — but we are getting lucky in three places**
(image tag drift, operator-managed route names, and broker intersection
semantics), all flagged in the watch list at the bottom.

---

## 1. Where we deviate from the guide deliberately — and why

### 1.1 Client-plane AuthPolicy attachment (guide §5.1.4.1) — guide instruction does not work for our topology

The guide says the gateway-level AuthPolicy "must" target the Gateway's
`mcp` listener (`sectionName: mcp`). On our topology (operator-created
two-listener Gateway, `data-science-gateway-class`, broker with
`privateHost`), **a policy attached to the `mcp` listener never executes
for `/mcp` traffic**: the ext_proc router runs first in the Envoy filter
chain and rewrites the Host header to the broker's derived host
(`mcp.mcp.local`) before Kuadrant's wasm auth evaluates, so client
traffic matches the operator-managed broker HTTPRoute, not the listener.
The failure is silent — 401s still work (backend default), but no
wristband is issued and all users see all tools. This cost us a full
diagnostic cycle (run #1).

Workshop practice (Module 8): attach the client policy to **HTTPRoute
`mcp-gateway-route`**. The guide authors may have seen listener
attachment work on their Helm-chart/`openshift-default` topology, but on
RHCL 1.4 + MCP Gateway 0.6/0.7 with the operator-managed broker route,
route attachment is the configuration that demonstrably enforces.

### 1.2 Tool names: prefixed (guide §5.2.3.2, §5.2.4.1) vs. unprefixed (v0.7.0 reality)

The guide instructs `toolPrefix: openshift_` and prefixed names in every
MCPVirtualServer (`openshift_pods_list`). As of MCP Gateway v0.7.0 the
prefix is applied **only on name conflicts** — tools keep natural names,
and prefixed VMS entries match nothing (0 tools for everyone, run #5).
Workshop practice: keep `toolPrefix` set (it is immutable and still used
on conflicts) but list **unprefixed** names in VMS resources, Keycloak
client roles, and Rego. Note the guide's own findings file (#15) already
knew the wristband claim needs unprefixed names — v0.7.0 extended that
to everything.

### 1.3 Authorization header: their bug is our feature (findings #7, guide §5.1.5.2)

The findings treat "JWT forwarded to the OpenShift MCP server" as a bug
("server uses it for K8s API auth and fails") and strip it everywhere.
We strip it **by default** (backend-plane policy, fail-closed) but
deliberately **pass it through** on the OpenShift MCP server's per-route
policy — because with External OIDC (Module 7), the Keycloak JWT *is* a
valid K8s API token, and the forwarded JWT is exactly what produces
per-user RBAC and audit attribution. Same mechanism, opposite intent,
enabled by a configuration (External OIDC) the guide doesn't cover.
One subtlety the findings miss: stripping on the backend default is
load-bearing for the broker's internal hairpin `initialize` — removing
it breaks `tools/call` for every server.

### 1.4 Identity model: groups + per-server client roles vs. realm roles (guide §5.1.3.3)

The guide's realm import uses two realm roles (`mcp-user`/`mcp-admin`)
and `redirectUris: ["*"]`. We use groups (VMS routing) plus **bearer-only
clients named after MCPServerRegistrations holding per-tool client
roles** (wristband source) — which is what the guide's own wristband
section (§5.1.4.2) actually requires; its example realm just doesn't
implement it. We also scope every client's redirect URIs (wildcard
redirect on a confidential client is a token-exfiltration foot-gun).

### 1.5 Gateway install: operator + explicit CRs vs. Helm charts (guide §5.1.2)

The guide installs via `mcp-gateway` + `mcp-gateway-ingress` Helm charts
with `gatewayClassName: openshift-default`. We create the Gateway,
MCPGatewayExtension, and Route explicitly (Module 2) on
`data-science-gateway-class`. Consequence: we hit findings #3 (broker
hardcodes `<gateway>-istio` service name) head-on, and the `privateHost`
patch in Module 2 is **required** — the guide's charts sidestep it by
naming convention. Our choice is deliberate: students see every resource.

### 1.6 Vault: dev mode over TLS-hardened (findings #13, guide §5.1.6)

Findings #13 (Vault needs the ingress CA bundle for JWKS TLS trust) does
not apply to us because Module 11 runs Vault in dev mode over in-cluster
HTTP — documented as NOT production. The audience-mapper half of #13 we
do implement (Module 6 realm script; `bound_audiences: mcp-gateway`).
Our two-stage AuthPolicy metadata pipeline matches guide §5.1.6.7,
upgraded with a CEL guard predicate so a failed Vault login fails the
request cleanly. One guide claim our Module 11 *disproves*: per-user
GitHub identity does work with the official `github-mcp-server` via
per-request `Authorization` header injection (M6 milestone verifies
distinct identities per user) — read-only mode remains wise for the
shared discovery PAT, but header injection is not hypothetical.

---

## 2. Where we are aligned (validated on live clusters)

| Findings item | Workshop status |
|---|---|
| #1 broken catalog image | Module 4 uses the same quay.io workaround image |
| #2 Playground must traverse ext_proc | Module 18 points the ConfigMap at the public gateway URL (equivalent to their internal-Istio-svc fix; both avoid the broker-direct path) |
| #3 `privateHost` | Module 2 REQUIRED patch |
| #5 broker no auto-reload | Module 5 + CLAUDE.md: rollout restart after registration changes |
| #6 lifecycle operator OOM 128Mi | Module 2: patch to 512Mi (answers file decision) |
| #8 MaaS needs Kuadrant CRDs first | Module sequencing satisfies it (Module 1 before 13) |
| #10 RHBK OwnNamespace | Module 6 installs into `keycloak` ns with its own OperatorGroup |
| #11 `toolPrefix` immutable | Documented in CLAUDE.md and Module 5 |
| #12 `scope=openid groups` | On every token request in every module + answers file |
| #14 dedicated gateway | MCP gateway is its own Gateway, separate from MaaS and Data Science gateways |
| #17 single-level subdomains | Our own lesson too (`mcp-openshift.<domain>`, `*.mcp.local` internal) |
| #18 streamable-http transport | Module 9: `claude mcp add -t http` |
| #19 ClusterLogForwarder 3-layer RBAC | Module 19 implements all three |
| #20 `openshift-gateway` Istio revision | Module 19 telemetry targets it; CLAUDE.md lesson |
| #21 wristband+VMS intersection enforces | Module 8/9 use both, plus per-route Rego at Authorino as a third layer the guide doesn't have |

On #21, one workshop addition the guide should adopt: a denied
`tools/call` surfaces as a **broker HTTP 500** wrapping the 403 on the
hairpin initialize ("server returned 4xx for initialize POST") — the
outer code lies, and knowing that saves hours.

---

## 3. Where we are getting lucky — watch list

1. **OpenShift MCP server image is `:latest`** (Module 4,
   `openshift-mcp-server-release-03:latest`). The tool list is
   image-dependent — this already bit us once (`pods_run` vs.
   `resources_create_or_update` swept through Keycloak roles, VMS lists,
   and docs). A repush of `:latest` can silently change the tool
   surface again and 0-out users whose roles no longer match.
   *Hardening: pin by digest once a known-good digest is identified, and
   keep the M2/M5 milestone checks (they catch drift) as the tripwire.*

2. **The client policy hangs off an operator-managed route name**
   (`mcp-gateway-route`). An operator upgrade that renames or
   restructures the broker route silently detaches the policy — the
   identical silent failure as run #1 (auth still 401s, filtering gone).
   *Mitigation already in place: M4/M5 milestone checks compare per-user
   tool counts, which catches it. Keep running them after any operator
   bump.*

3. **`tools/call` enforcement rests on broker intersection semantics**
   (wristband ∩ VMS), an empirically-validated v0.6/0.7 behavior that
   the guide's own findings initially got wrong (#4, #21 corrected).
   A broker release could weaken it without any API change. *Mitigation:
   our per-route OPA Rego at Authorino enforces independently of the
   broker (defense in depth the guide lacks), and the M5 denial check
   verifies the end-to-end denial on every run.*

4. **Tech-preview catalog ships latest-only** for the MCP Gateway
   operator — the v0.6→0.7 toolPrefix surprise will have sequels. Not
   pinnable; the durable rule is documented (VMS lists and Keycloak
   roles must match actual `tools/list` output) and M2 verifies it.

5. **No public OIDC client for browser flows.** The guide (§5.1.3.3)
   provisions a public `mcp-playground` client; our realm has only
   confidential/bearer-only clients (plus `oc-cli`/`rhoai-gateway` for
   other purposes), and Module 18 acquires Playground tokens via
   password grant against the confidential client. Works for the
   workshop; a real deployment wiring Gen AI Studio SSO would want the
   public client. *Low priority; note only.*

6. **Version skew vs. the guide baseline** (their appendix: SM 3.3.2,
   RHCL 1.3.2, MCP GW v0.6.0): we now pin RHCL 1.3.4 and run SM 3.3.3 +
   MCP GW v0.7.0. Every behavioral delta we've found is documented, but
   the guide itself should be read as a v0.6.0 document — anything in it
   that contradicts observed v0.7.0 behavior (notably prefixes) is
   resolved in favor of the cluster.

---

## 4. Corrections the guide authors would want back

Worth feeding upstream to the guide/findings owners:

- §5.1.4.1's `sectionName: mcp` instruction silently fails on the
  operator-managed broker-route topology — attach to the broker
  HTTPRoute instead (and the failure mode deserves a warning: 401s keep
  working while filtering vanishes).
- All prefixed-tool-name examples (§5.2.3.2, §5.2.4.1) are stale as of
  v0.7.0 (prefix-on-conflict-only).
- Findings #7's "strip Authorization" workaround forecloses the
  per-user K8s identity pattern; with External OIDC the forwarded JWT is
  the feature. The strip is right as a *default*, wrong as an absolute.
- The findings' GitHub-MCP assessment understates it: per-user PATs via
  AuthPolicy header injection work against the official image
  (validated, M6).
- A denied call surfaces as broker 500 wrapping the inner 403 — worth a
  troubleshooting entry.
