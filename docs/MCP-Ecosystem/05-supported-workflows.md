## **5\. Supported Workflows**

### **5.1 Setup Workflows**

**Video walkthrough:** The [MCP Ecosystem Setup Demo](https://youtu.be/ncwSVtc_6Qo) covers the full platform engineer setup flow described in sections 5.1.1–5.1.7.

#### 5.1.1 Installing and Configuring Operators

The MCP Ecosystem requires two tiers of operators. The first tier consists of platform-level prerequisites that must be installed before any MCP-specific setup. The second tier consists of MCP-specific operators installed as part of the ecosystem setup.

##### **5.1.1.1 Tier 1 — Platform Prerequisites**

These operators must be installed via OperatorHub before proceeding with MCP ecosystem setup. They provide foundational capabilities that the MCP components depend on:

| Operator | OperatorHub Source | Channel | Role in the MCP Ecosystem |
| :---- | :---- | :---- | :---- |
| OpenShift Service Mesh (Istio)\* | `redhat-operators` | `stable` | Programs Envoy proxies for the MCP Gateway; handles sidecar injection and mTLS between gateway components |
| cert-manager | `certified-operators` | `stable-v1` | Manages TLS certificates and ClusterIssuers for gateway Routes and inter-component communication |
| Red Hat Connectivity Link | `redhat-operators` | `stable` | Installs Kuadrant, Authorino, and Limitador — the policy engine used by AuthPolicy CRs for authentication, authorization, and rate limiting |
| Red Hat OpenShift AI (RHOAI) | `redhat-operators` | See note | Provides the DataScienceCluster, OGX operator, Gen AI Studio dashboard, and model serving infrastructure |

\*NOTE: You may also just install the Istio gateway controller if full service mesh is not required, at the cost of security features provided by the service mesh (automatic mTLS between pods).

RHOAI may require a custom `CatalogSource` for early-access versions (e.g., 3.4.0). Consult the RHOAI release notes for the correct catalog image and subscription configuration.

##### **5.1.1.2 Tier 2 — MCP Ecosystem Operators**

These are installed as part of the MCP ecosystem setup process:

| Component | Install Method | What It Does |
| :---- | :---- | :---- |
| MCP Gateway Operator | OLM (custom CatalogSource \+ Subscription) | Watches MCPGatewayExtension CRs; deploys and manages the MCP broker/router |
| Red Hat Build of Keycloak (RHBK) | OLM (Subscription) | Manages Keycloak instances and realm imports for identity management |
| MCP Lifecycle Operator | Manual YAML apply | Watches MCPServer CRs; creates downstream Deployments, Services, and health checks |
| HashiCorp Vault | Helm chart (`hashicorp/vault`) with `global.openshift: true`. Supported by HashiCorp for OpenShift; Red Hat certification covers the Vault Secrets Operator (VSO) only, not the server itself. | Provides dynamic secret storage for per-user credential injection |

##### **5.1.1.3 Post-Install Activation**

After operator installation, the following activation steps are required before proceeding to gateway and identity setup:

* **Kuadrant CR:** Apply a `Kuadrant` custom resource to activate the Authorino and Limitador control plane. This depends on Connectivity Link being installed:

> [snippets/05-supported-workflows/post-install-activation.yaml](snippets/05-supported-workflows/post-install-activation.yaml)

```
apiVersion: kuadrant.io/v1beta1
kind: Kuadrant
metadata:
  name: kuadrant
  namespace: openshift-operators
spec: {}
```

* **DataScienceCluster configuration:** Ensure the `DataScienceCluster` CR has `llamastackoperator: Managed` to enable the OGX operator.  
* **Dashboard feature flags:** Patch the `OdhDashboardConfig` to enable `genAiStudio` and `mcpCatalog` features in the RHOAI dashboard.

> [snippets/05-supported-workflows/post-install-activation.sh](snippets/05-supported-workflows/post-install-activation.sh)

```
        oc patch odhdashboardconfigs odh-dashboard-config \
    -n redhat-ods-applications \
    --type=merge \
    -p '{"spec":{"dashboardConfig":{"mcpCatalog":true,"genAiStudio":true}}}'
```

**Note:** The `genAiStudio` flag is documented in the RHOAI 3.4 dashboard configuration reference. The `mcpCatalog` flag exists in the ODH Dashboard source code but is not yet listed in the official RHOAI documentation and may change without notice.

Detailed configuration of each component (Keycloak realms, Vault policies, Gateway resources, AuthPolicies) is covered in the sections that follow.

#### 5.1.2 Setting Up the MCP Gateway Namespace

This section covers provisioning a gateway namespace — the namespace where the MCP Gateway, broker, Route, and associated resources will live. The namespace layout depends on the chosen gateway topology (see section 7): in a shared gateway topology, one namespace holds all teams' resources; in a gateway-per-team topology, this process is repeated for each team.

##### **5.1.2.1 Create the gateway namespace**

Create a dedicated namespace for the gateway and its associated resources. Depending on the topology, this namespace may contain resources for a single team or for all teams sharing the gateway.

##### **5.1.2.2 Install the Gateway via Helm**

The `mcp-gateway` Helm chart creates two primary resources:

* A **Gateway CR** using `gatewayClassName: openshift-default`, which causes Istio to program an Envoy proxy for the gateway.  
* An **MCPGatewayExtension CR** referencing the Gateway's listener section. This tells the MCP Gateway operator to deploy a broker and router for this gateway instance.

Key configuration in the Helm values:

* `controller.enabled: false` — the MCP Gateway operator runs cluster-wide (installed in 3.1.1); the chart should not deploy its own controller.
This is specific to the OLM installation path (section 5.1.1.2) where the operator runs cluster-wide. The upstream Helm chart defaults to `controller.enabled: true` because its primary installation path includes the controller in the chart.  
* `publicHost` — the externally reachable hostname for this gateway. Must match the cluster's apps domain (e.g., `<gateway-name>.<cluster-apps-domain>`).  
* `mcpGatewayExtension.gatewayRef` — references the Gateway by name, namespace, and listener section name.

##### **5.1.2.3 Create the OpenShift Route**

The Helm chart creates the Gateway and MCPGatewayExtension CRs. Once reconciled, the MCP Gateway controller creates the broker Deployment, Service, EnvoyFilter (for ext\_proc routing), and an internal HTTPRoute. Note that the `openshift-default` GatewayClass is handled by Istio on clusters with Service Mesh — this is required for the ext\_proc/EnvoyFilter mechanism. On clusters without Service Mesh, the GatewayClass falls back to HAProxy, and ext\_proc will not work.

A second Helm chart (`mcp-gateway-ingress`) creates an OpenShift Route that exposes the gateway externally with edge TLS termination. The Route hostname must match the `publicHost` configured in the Gateway Helm values.

##### **5.1.2.4 Generate wristband signing keys**

The MCP Gateway uses a wristband mechanism to propagate verified identity claims from the Authorino policy engine to the MCP broker. This allows the broker to filter `tools/list` responses based on the caller's identity without re-evaluating authorization.

The wristband requires an ECDSA key pair:

* A **private key** secret is created in the namespace where Authorino runs. Authorino uses this to sign a wristband JWT containing the caller's verified identity claims after a successful AuthPolicy evaluation.  
* A **public key** secret is created in the gateway namespace. The broker uses this to verify the wristband signature.

After creating the key secrets, the MCPGatewayExtension is patched to reference the public key secret via `spec.trustedHeadersKey`. This links the broker to the verification key.

**Shortcut:** The MCPGatewayExtension CRD also supports `spec.trustedHeadersKey.generate: Enabled`, which causes the operator to automatically create both key secrets with owner references — eliminating the manual key generation steps above.

##### **5.1.2.5 Wait for broker readiness**

Once the MCPGatewayExtension is reconciled, the MCP Gateway operator deploys the broker and creates a config Secret in the gateway namespace. This Secret contains the broker's upstream server list and is updated automatically as MCP servers are registered. The gateway namespace is ready for use once this Secret appears.

Registering the gateway with Gen AI Studio (the `gen-ai-aa-mcp-servers` ConfigMap) is covered in section 5.1.7.

#### 5.1.3 Configuring Keycloak for Identity Management

Keycloak serves as the central OIDC identity provider for the MCP Ecosystem. All authentication — whether from Gen AI Studio, programmatic MCP clients, or the Vault credential injection flow — relies on JWTs issued by Keycloak. This section covers the full Keycloak setup.

##### **5.1.3.1 Deploy Keycloak**

The RHBK operator (installed in 3.1.1) manages Keycloak instances declaratively. The setup requires:

* A **PostgreSQL database** for persistent Keycloak state (a Deployment, Service, PVC, and credentials Secret). The RHBK operator does not provision this database during installation — it must be set up separately.  
* A **Keycloak CR** that references the database and specifies the externally reachable hostname. The RHBK operator reconciles this into a StatefulSet, Services, and an OpenShift Route.

The Keycloak hostname must match the cluster's apps domain. This is environment-specific and must be adjusted for each cluster.

##### **5.1.3.2 Identity federation with OpenShift**

In environments where users already authenticate to OpenShift, Keycloak can be configured as an identity broker that delegates authentication to the OpenShift OAuth server. Users log in with their existing OpenShift credentials, and Keycloak maps their OpenShift identity into the MCP realm — including group memberships if OpenShift groups are synchronized.

This avoids maintaining a separate user directory in Keycloak and aligns MCP access with existing cluster RBAC. The seed users and passwords described below are primarily useful for standalone testing; production deployments should prefer identity federation.

##### **5.1.3.3 Import the realm**

A **KeycloakRealmImport CR** creates the MCP realm with:

* **Realm roles** — define permission levels (e.g., a basic user role and an admin role). These are included in JWTs and can be referenced by AuthPolicy CEL predicates.  
* **Clients** — two OIDC clients are required:  
* A **confidential client** for the MCP Gateway broker. Used for service-to-service authentication with a client secret.  
* A **public client** for Gen AI Studio / Playground and other end-user-facing tools. Uses authorization code flow with no client secret.  
* **Seed users** — initial users with passwords and realm role assignments for testing and validation.  
* **Client scopes and protocol mappers** — a `roles` scope with mappers that include `realm_access.roles` and `resource_access.{client_id}.roles` in both access and ID tokens.

An example KeycloakRealmImport CR (`v2alpha1` is correct for Red Hat Build of Keycloak; upstream community Keycloak has promoted to `v2beta1`):

> [snippets/05-supported-workflows/import-the-realm.yaml](snippets/05-supported-workflows/import-the-realm.yaml)

```
apiVersion: k8s.keycloak.org/v2alpha1
kind: KeycloakRealmImport
metadata:
  name: mcp-gateway-realm
  namespace: keycloak
spec:
  keycloakCRName: keycloak
  realm:
    realm: mcp-gateway
    enabled: true
    roles:
      realm:
        - name: mcp-user
          description: Basic MCP gateway access
        - name: mcp-admin
          description: Full MCP gateway access
    users:
      - username: mcp-user1
        firstName: MCP
        lastName: User
        email: user1@example.com
        enabled: true
        credentials:
          - type: password
            value: user1pass
            temporary: false
        realmRoles:
          - mcp-user
      - username: mcp-admin1
        firstName: MCP
        lastName: Admin
        email: admin1@example.com
        enabled: true
        credentials:
          - type: password
            value: admin1pass
            temporary: false
        realmRoles:
          - mcp-user
          - mcp-admin
    clients:
      - clientId: mcp-gateway
        enabled: true
        protocol: openid-connect
        publicClient: false
        secret: mcp-gateway-client-secret
        redirectUris: ["*"]
        standardFlowEnabled: true
        directAccessGrantsEnabled: true
        serviceAccountsEnabled: true
        defaultClientScopes: [openid, profile, email, roles]
      - clientId: mcp-playground
        enabled: true
        protocol: openid-connect
        publicClient: true
        redirectUris: ["*"]
        standardFlowEnabled: true
        directAccessGrantsEnabled: true
        defaultClientScopes: [openid, profile, email, roles]
    clientScopes:
      - name: roles
        protocol: openid-connect
        protocolMappers:
          - name: realm-roles
            protocol: openid-connect
            protocolMapper: oidc-usermodel-realm-role-mapper
            config:
              access.token.claim: "true"
              claim.name: realm_access.roles
              id.token.claim: "true"
              jsonType.label: String
              multivalued: "true"
              userinfo.token.claim: "true"
          - name: client-roles
            protocol: openid-connect
            protocolMapper: oidc-usermodel-client-role-mapper
            config:
              access.token.claim: "true"
              claim.name: "resource_access.${client_id}.roles"
              id.token.claim: "true"
              jsonType.label: String
              multivalued: "true"
              userinfo.token.claim: "true"
```

**Workshop note:** Both clients above have `directAccessGrantsEnabled: true`, which enables the Resource Owner Password Credentials (ROPC) grant used in the token acquisition examples (section 5.2.5). ROPC is deprecated per RFC 9700 (OAuth 2.0 Security Best Current Practice, March 2025) and disabled by default in Keycloak 26.2+. For production deployments, disable ROPC and use the authorization code flow with PKCE or the device authorization grant instead.

**Security note:** The `redirectUris: ["*"]` configuration is a workshop convenience. Wildcard redirect URIs have been the root cause of multiple Keycloak CVEs (CVE-2023-6927, CVE-2024-8883, CVE-2026-7504) and are forbidden by RFC 9700. Production deployments must replace `["*"]` with explicit redirect URIs.

##### **5.1.3.4 Create groups for tool routing**

Groups are one mechanism the gateway AuthPolicy can use to route users to the correct MCPVirtualServer. In this approach, each group maps to a tool subset — the gateway-level AuthPolicy reads the `groups` claim from the JWT and sets the `x-mcp-virtualserver` header accordingly, directing the broker to the right MCPVirtualServer for that user. Alternatively, routing can be based on realm roles (`realm_access.roles`) or other JWT claims, depending on how the organization models access. For details on how MCPVirtualServers filter tools via the `X-Mcp-Virtualserver` header, see the [MCP Gateway Virtual Servers guide](https://docs.kuadrant.io/latest/mcp-gateway/docs/guides/virtual-mcp-servers/). For the underlying Authorino mechanism of extracting JWT claims and using them in authorization decisions (the pattern behind reading groups and injecting routing headers), see the [Token normalization guide](https://docs.kuadrant.io/latest/authorino/docs/user-guides/token-normalization/). For configuring AuthPolicy CEL expressions that enforce tool-level authorization, see the [MCP Gateway Authorization guide](https://docs.kuadrant.io/latest/mcp-gateway/docs/guides/authorization/).

How groups are provisioned depends on the identity source:

* **Standalone Keycloak:** Groups are created via the Keycloak Admin API after the realm import. Each group is assigned the appropriate users. A **groups protocol mapper** is added to the public client so the `groups` claim appears in issued tokens.  
* **Federated from OpenShift:** OpenShift groups are synchronized into Keycloak through the identity broker. The OpenShift group names must align with what the gateway AuthPolicy expects in the JWT `groups` claim. Verify that the federation mapping passes group memberships through to tokens — this may require configuring a group mapper on the identity provider in Keycloak.

In either case, the group structure should mirror the MCPVirtualServer resources that will be created in section 5.1.5. For example, if there are three MCPVirtualServers representing different tool subsets (admin tools, specialized tools, basic tools), there should be three corresponding groups.

**Required setup:** The `groups` claim is not included in Keycloak tokens by default. To make it available:

1. Create a custom client scope named `groups` in the MCP realm.
2. Add an `oidc-group-membership-mapper` protocol mapper to this scope, configured with `claim.name: "groups"`, `full.path: "false"`, and token claim flags enabled (`access.token.claim`, `id.token.claim`, `userinfo.token.claim` all set to `"true"`).
3. Assign this scope to the public client (`mcp-playground`). If assigned as a **Default** scope, the `groups` claim is included automatically in all tokens. If assigned as an **Optional** scope, clients must explicitly request `scope=openid groups` (as shown in section 5.2.5.1).

Without this setup, JWTs will not carry the `groups` claim, and MCPVirtualServer routing based on group membership will fail silently — users will see an empty tool list with no error.

#### 5.1.4 Configuring Gateway-Level Authentication (AuthPolicy)

The gateway-level AuthPolicy targets the Gateway CR's MCP listener section and applies to every request that enters the gateway. In this configuration, the policy addresses three concerns: JWT authentication, wristband signing, and MCPVirtualServer routing.

##### **5.1.4.1 JWT authentication**

The AuthPolicy's `authentication` block configures JWT validation against the Keycloak realm's OIDC discovery endpoint. Every incoming request must carry a valid Bearer token issued by the configured Keycloak realm. Requests without a token or with an expired/invalid token are rejected with a 401 before reaching any MCP server.

> [snippets/05-supported-workflows/jwt-authentication.yaml](snippets/05-supported-workflows/jwt-authentication.yaml)

```
apiVersion: kuadrant.io/v1
kind: AuthPolicy
metadata:
  name: mcp-gateway-auth
spec:
  targetRef:
    group: gateway.networking.k8s.io
    kind: Gateway
    name: <gateway-name>
    sectionName: mcp
  rules:
    authentication:
      keycloak-jwt:
        jwt:
          issuerUrl: https://<keycloak-route>/realms/mcp-gateway
```

The `sectionName` must be `mcp` — the external-facing listener where client requests enter the gateway. The `mcps` listener is a separate backend listener used by individual MCP server HTTPRoutes for internal routing (see section 5.2.3).

##### **5.1.4.2 Wristband signing for per-tool authorization**

The gateway-level AuthPolicy includes an OPA Rego authorization block that extracts per-server tool roles from the JWT's `resource_access` claim. Each MCP server registered with Keycloak as a client can define client roles representing individual tools. The Rego policy builds a map of `{server: [tools]}` from these claims.

This map is then signed into a **wristband** — a short-lived ES256 JWT placed in the `x-mcp-authorized` header. The wristband is signed with the private key created during gateway setup (section 5.1.2) and verified by the broker using the corresponding public key. The broker uses this to filter `tools/list` and `tools/call` to only the tools the caller is authorized to use.

The wristband provides fine-grained, per-tool authorization without requiring the broker to re-evaluate the full AuthPolicy on every request.

**Note:** The wristband carries an `allowed-capabilities` claim containing a JSON-encoded map of authorized tools per server (e.g., `{"tools":{"server-route":["tool1","tool2"]}}`). The upstream MCP Gateway examples use Authorino's native `wristband` response type with CEL selectors rather than OPA Rego to assemble this claim. Either approach works as long as the claim name and structure match what the broker expects.

##### **5.1.4.3 MCPVirtualServer routing**

The same AuthPolicy sets the `x-mcp-virtualserver` response header using a CEL expression that reads the `groups` claim from the authenticated JWT. The expression evaluates the user's group membership and maps it to the name of the appropriate MCPVirtualServer resource. The broker uses this header to determine which tool subset to present in `tools/list`.

This requires that tokens are requested with `scope=openid groups` so that the JWT carries the `groups` claim. Without this scope, the CEL expression will not find any group memberships and routing will fail silently — users will see an empty tool list with no error.

> [snippets/05-supported-workflows/virtualmcpserver-routing.txt](snippets/05-supported-workflows/virtualmcpserver-routing.txt)

```
# In the gateway-level AuthPolicy response rules
spec:
  rules:
    response:
      success:
        headers:
          x-mcp-virtualserver:
            expression: |
              auth.identity.groups.exists(g, g == 'team-leads')
                ? '<namespace>/lead-tools'
                : auth.identity.groups.exists(g, g == 'team-ops')
                  ? '<namespace>/ops-tools'
                  : '<namespace>/dev-tools'
```

The CEL ternary evaluates group membership in priority order and resolves to the namespaced name of the appropriate MCPVirtualServer. Users not matching any group receive an empty value.

This is a coarse-grained, group-level tool curation layer. It controls which tools *appear* in discovery. The wristband layer (above) controls which tools can actually be *called*. Both layers work together — the broker returns the intersection.

#### 5.1.5 Configuring Per-Tool Authorization (CEL Predicates)

Beyond the gateway-level AuthPolicy, individual MCP servers can have their own AuthPolicies that target specific HTTPRoutes. These per-server policies add additional authorization constraints on top of the gateway-level JWT validation.

##### **5.1.5.1 Per-server access restriction**

A per-server AuthPolicy targets an HTTPRoute (rather than the Gateway) and can restrict which users or groups are allowed to reach that specific MCP server. For example, an AuthPolicy on the OpenShift MCP server's HTTPRoute can require that the caller belong to an admins group — even if the gateway-level policy already authenticated the user and the MCPVirtualServer includes OpenShift tools.

The authorization block uses `patternMatching` with a selector on `auth.identity.groups` to check group membership. This acts as a hard access control: if the user's JWT doesn't contain the required group, the request is rejected with a 403 at the HTTPRoute level, before it reaches the MCP server.

##### **5.1.5.2 Header manipulation**

Per-server AuthPolicies can also modify request headers before they reach the MCP server. A common pattern is clearing or replacing the `Authorization` header in the success response — this prevents the user's Keycloak JWT from being forwarded to a server that might misinterpret it as an upstream credential. Depending on the server, the desired behavior may be to fall back to a ServiceAccount token, use a credential injected by Vault (section 5.1.6), or operate with no authorization header at all.

##### **5.1.5.3 Per-tool authorization via OPA Rego**

For fine-grained control where different user groups need access to different tools on the same server, per-route AuthPolicies can use OPA Rego rules that check the `x-mcp-toolname` header. This header is injected by the router (ext\_proc) during request processing — the client never sets it. The router extracts the tool name from the JSON-RPC `tools/call` payload and places it in the header before the AuthPolicy evaluates.

> [snippets/05-supported-workflows/per-tool-authorization-via-opa-rego.yaml](snippets/05-supported-workflows/per-tool-authorization-via-opa-rego.yaml)

```
spec:
  rules:
    authorization:
      tool-acl:
        opa:
          rego: |
            allow {
              some g in input.auth.identity.groups
              g == "developers"
              input.request.headers["x-mcp-toolname"] in ["greet", "time", "headers"]
            }
            allow {
              some g in input.auth.identity.groups
              g == "ops"
              input.request.headers["x-mcp-toolname"] in ["add_tool", "slow"]
            }
            allow {
              some g in input.auth.identity.groups
              g == "leads"
            }
```

This creates a matrix where developers can call a specific subset of tools, ops can call a different subset, and leads can call any tool. The Rego rules are evaluated per-request, so tool access is enforced at call time — not just at discovery time.

##### **5.1.5.4 Vault credential injection**

For MCP servers that call external APIs requiring per-user credentials (e.g., a GitHub MCP server needing each user's personal access token), a per-server AuthPolicy can perform dynamic credential injection using Vault metadata evaluators. This is covered in detail in section 5.1.6.

##### **5.1.5.5 Layered authorization model**

The full authorization stack operates in layers:

1. **Gateway-level JWT validation** — is the token valid? (401 if not)  
2. **Per-server patternMatching** — is this user allowed to reach this server? (403 if not)  
3. **Wristband tool filtering** — which specific tools can this user call? (broker filters `tools/list` and rejects unauthorized `tools/call`)  
4. **MCPVirtualServer routing** — which curated tool subset does this user see? (broker selects the tool view)

Layers 1 and 2 are hard enforcement (requests are rejected). Layers 3 and 4 are filtering (tools are hidden or restricted). A misconfigured MCPVirtualServer cannot grant access to tools the user is not authorized for via the wristband — the broker enforces the intersection.

#### 5.1.6 Setting Up Vault for Credential Injection (Optional)

Some MCP servers need to call external APIs that require per-user credentials — for example, a GitHub MCP server that needs each user's personal access token. Rather than embedding shared credentials in the server configuration or requiring users to supply their own, the MCP Ecosystem supports dynamic credential injection through HashiCorp Vault. The gateway exchanges the user's identity token for the appropriate backend credential at request time, transparently to both the user and the MCP server.

##### **5.1.6.1 Enable the secrets engine**

After Vault is initialized and unsealed, enable a KV v2 secrets engine at a dedicated mount path for MCP credentials. This is where per-user secrets will be stored, organized by username.

##### **5.1.6.2 Add Vault-required claims within Keycloak**

If Vault credential injection will be used, the Keycloak public client needs additional protocol mappers to ensure the JWT carries the claims Vault requires for identity-based secret access:

* **`sub`** — the user's unique identifier. Vault uses this as the entity alias name.  
* **`preferred_username`** — the user's login name. Vault uses this in policy path templating to scope secret access (e.g., `secret/data/mcp-gateway/users/{preferred_username}/...`).  
* **Audience mapper** — ensures the token's `aud` claim includes the public client ID. Vault's JWT auth method validates this to confirm the token was intended for MCP use.

These mappers are added to the public client via the Keycloak Admin API. Without them, Vault JWT authentication will reject the token or fail to resolve the correct secret path.

##### **5.1.6.3 Configure JWT authentication**

Vault must be configured to trust JWTs issued by Keycloak. This involves creating a JWT auth method that points at the Keycloak realm's JWKS endpoint for token validation. The auth method configuration includes:

* The JWKS URL from the Keycloak realm's OIDC discovery metadata.  
* A Vault role that specifies which JWT audiences are accepted (matching the Keycloak client IDs used by the gateway and playground).  
* Claim mappings that extract `preferred_username` from the JWT. Vault uses this in policy path templating to scope secret access per user.

##### **5.1.6.4 Create a secrets policy**

A Vault policy restricts which paths each authenticated user can read. Using Vault's templated policies, the policy grants read access only to paths scoped by the caller's username — for example, `secret/data/mcp-gateway/users/{{identity.entity.aliases.<mount>.metadata.preferred_username}}/*`. This ensures that even if a user authenticates to Vault successfully, they can only read their own credentials.

**Important:** The `<mount>` placeholder in the policy template must be the Vault **mount accessor** (e.g., `auth_jwt_abc123`), not the mount path (e.g., `jwt`). Obtain the accessor with `vault auth list`. Using the mount path instead of the accessor causes the template to silently fail to resolve, resulting in denied access.

##### **5.1.6.5 Store per-user credentials**

Per-user credentials (API keys, personal access tokens, OAuth tokens) are stored at the policy-scoped path for each user. This document assumes that cluster administrators have an established process for provisioning user credentials into Vault. The credential injection mechanism described below is agnostic to how secrets are populated — it only requires that a secret exists at the expected path when a tool call is made.

##### **5.1.6.6 Secret path organization**

Secrets can be organized at two levels depending on the use case:

* **Per-user credentials:** `secret/data/mcp-gateway/users/{sub}/{server-name}` — scoped by the user's `sub` or `preferred_username` claim. Each user has their own credential for a given server (e.g., personal GitHub PATs).  
* **Team-level credentials:** `secret/data/mcp-gateway/teams/{group}/{server-name}` — scoped by Keycloak group. All members of a team share the same credential (e.g., a shared API key for an internal service).

The AuthPolicy CEL expressions determine which path is read at request time based on the JWT claims.

##### **5.1.6.7 AuthPolicy wiring for credential injection**

A per-server AuthPolicy on the MCP server's HTTPRoute performs dynamic credential injection using a two-stage metadata evaluation pipeline:

1. **Stage 0 — Vault login:** The AuthPolicy extracts the user's Keycloak JWT from the `Authorization` header and sends it to Vault's JWT auth endpoint. Vault validates the token against the configured JWKS and returns a Vault client token.  
2. **Stage 1 — Secret retrieval:** Using the Vault client token from Stage 0, the AuthPolicy reads the user's credential from the KV store. The URL is constructed dynamically using claims from the authenticated identity.  
3. **Response — Header injection:** The retrieved credential is injected as a request header. The MCP server receives the request with the backend credential already in place — it does not need to know about Vault, Keycloak, or the credential exchange.

> [snippets/05-supported-workflows/authpolicy-wiring-for-credential-injection.txt](snippets/05-supported-workflows/authpolicy-wiring-for-credential-injection.txt)

```
# Per-route AuthPolicy with Vault credential exchange
spec:
  rules:
    metadata:
      vault-login:
        http:
          url: "https://vault.vault.svc:8200/v1/auth/jwt/login"
          method: POST
          body:
            expression: |
              '{"jwt":"' + auth.identity.jwt + '","role":"authorino"}'
          headers:
            Content-Type:
              expression: "'application/json'"
        priority: 0
      vault-secret:
        http:
          url:
            expression: |
              'https://vault.vault.svc:8200/v1/secret/data/mcp-gateway/users/'
              + auth.identity.sub
          headers:
            X-Vault-Token:
              expression: "auth.metadata.vault-login.auth.client_token"
        priority: 1
    response:
      success:
        headers:
          x-user-credential:
            expression: "auth.metadata.vault-secret.data.data.api_key"
```

The `priority` field controls evaluation order — Stage 0 must complete before Stage 1 can use the Vault token. The response `expression` extracts the specific key from the Vault KV secret and injects it as a header.

MCP servers using this pattern **must accept credentials via HTTP headers** — not only environment variables. If a server only reads credentials from environment variables at startup, Vault credential injection cannot provide per-user isolation. The server must read the injected header (e.g., `X-User-Credential` or `Authorization`) on each incoming request.

If the user does not have a credential stored at the expected Vault path, the metadata evaluation fails and the request is rejected before reaching the MCP server.

##### **5.1.6.8 Reusability**

The Vault infrastructure (secrets engine, JWT auth, policy) is configured once. Each MCP server that needs credential injection gets its own per-server AuthPolicy with the appropriate Vault path. The pattern supports any credential type as long as it is stored in Vault under the user's path.

#### 5.1.7 Wiring Llama Stack/OGX and Gen AI Studio to the Gateway

This section covers connecting the AI stack to the MCP Gateway so that AI engineers can discover and invoke tools through the Gen AI Studio Playground.

##### **5.1.7.1 Register the gateway with the RHOAI Dashboard**

The RHOAI Dashboard reads a ConfigMap named `gen-ai-aa-mcp-servers` in the RHOAI applications namespace to populate the list of available MCP gateways. Each entry in the ConfigMap contains a JSON object with the gateway's URL (pointing to the `/mcp` path) and a human-readable description.

> [snippets/05-supported-workflows/register-the-gateway-with-the-rhoai-dashboard.yaml](snippets/05-supported-workflows/register-the-gateway-with-the-rhoai-dashboard.yaml)

```
apiVersion: v1
kind: ConfigMap
metadata:
  name: gen-ai-aa-mcp-servers
  namespace: redhat-ods-applications
data:
  MCP-Gateway: |
    {
      "url": "https://<mcp-gateway-route>/mcp",
      "description": "MCP Gateway with tools"
    }
```

This ConfigMap is what makes the gateway selectable as an MCP tool source when creating a Playground. In a gateway-per-team topology, each team's gateway gets its own entry. In a shared gateway topology, a single entry serves all teams.

##### **5.1.7.2 Prerequisites for model serving**

Before creating a Playground, the following must be in place:

* A **vLLM ServingRuntime** and **InferenceService** running in the team's namespace, serving a model that supports tool calling.  
* The InferenceService must be annotated with `opendatahub.io/genai-asset: "true"` so that Gen AI Studio can discover it.  
* The served model name must align with the InferenceService name.

These are standard RHOAI model serving prerequisites and are not MCP-specific.

##### **5.1.7.3 Create a Playground in Gen AI Studio**

The Playground is created through the Gen AI Studio UI in the RHOAI Dashboard. During creation, the user:

1. Selects the namespace where the Playground will be created (this should be the gateway namespace or the team's namespace).  
2. Selects the vLLM model endpoint to use for inference.  
3. Adds the MCP Gateway as a tool source — selecting the gateway entry registered via the ConfigMap.

When the Playground is created, the Dashboard creates a **Llama Stack (upstream now OGX) distribution** CR in the selected namespace. The OGX operator reconciles this into the necessary pods and configuration, wiring together the vLLM model endpoint and the MCP Gateway URL. Llama Stack/OGX acts as the orchestration layer — it receives chat messages from the Playground UI, decides when to invoke tools, routes tool calls through the gateway, and assembles the final response.

Once the Playground is running, AI engineers can authenticate with the gateway and begin using MCP tools. The consumption workflow — obtaining tokens, enabling tools, and interacting through the chat interface — is covered in section 5.2.5.

![Gen AI Studio Playground with MCP Gateway integration][image3]

### **5.2 Usage Workflows**

**Video walkthrough:** The [MCP Ecosystem Usage Demo](https://youtu.be/Fb3GXLJxfyQ) covers catalog browsing, server deployment, gateway registration, and Playground interaction described in sections 5.2.1–5.2.5.

#### 5.2.1 Discovering MCP Servers via the MCP Catalog

The MCP Catalog is a browsable inventory of available MCP servers integrated into the RHOAI Dashboard. It requires the `mcpCatalog` feature flag, which is enabled during platform setup (section 5.1.1). The Catalog provides a single place for AI engineers and platform engineers to discover what MCP servers are available before deploying them.

##### **5.2.1.1 Built-in and curated servers**

RHOAI ships with pre-populated catalog entries for supported MCP servers (e.g., the OpenShift MCP Server). Platform engineers can also register additional servers to make them discoverable through the same interface — this is covered in section 5.2.6.

![MCP Catalog browsing view with filters and server cards][image4]

##### **5.2.1.2 What the Catalog shows**

For each server, the Catalog displays:

* **Name and provider** — identifies the server and who maintains it.  
* **Description** — a summary of what the server does and what use cases it supports.  
* **Tools** — the list of tools the server exposes, with names, descriptions, and parameter schemas. This lets users evaluate whether a server's capabilities match their needs before deploying it.  
* **Container image artifact** — the OCI image URI used for deployment. Remote MCP servers don't have this field.  
* **Version and transport** — the server version and supported transport type (HTTP).  
* **License** — the license under which the server is distributed.  
* **README** \- Important instructions and pre-requisites for running the server.

![MCP Catalog server detail page showing metadata, tools, and deployment option][image5]

The catalog also supports filtering tools by name and description in the details page.

![Tool filtering by name in the MCP Catalog details page][image6]

##### **5.2.1.3 Catalog API**

For programmatic discovery — for example, from CI/CD pipelines or custom tooling — the Catalog also exposes REST endpoints:

* `GET /api/mcp_catalog/v1alpha1/mcp_servers` — lists all registered servers with metadata including name, description, version, artifacts, runtime metadata, and tool count.  
* `GET /api/mcp_catalog/v1alpha1/mcp_servers/{id}/tools` — returns the full tool list for a specific server, including parameter schemas.

##### **5.2.1.4 From discovery to deployment**

Once a user identifies a server they want to use, they can trigger deployment directly from the Catalog UI. The deployment workflow is covered in section 5.2.2.

#### 5.2.2 Deploying MCP Servers onto OpenShift

MCP servers are deployed onto OpenShift as `MCPServer` custom resources. The MCP Lifecycle Operator watches these CRs and reconciles them into the necessary Kubernetes resources (Deployment, Service, health checks). There are two paths to creating an `MCPServer` CR: through the Catalog UI or directly against the cluster.

##### **5.2.2.1 Deploying from the Catalog UI**

The **Deploy MCP server** button on the server detail page is gated on the presence of the `MCPServer` CRD on the cluster. If the CRD has not been installed (section 5.1.1), the button is disabled with a tooltip indicating that the CRD is not available. This prevents users from attempting a deployment that would fail silently.

![Deploy button disabled when MCPServer CRD is not installed on the cluster][image7]

Once the Lifecycle Operator is installed and the CRD is present, clicking **Deploy MCP server** opens the deployment modal. The modal collects:

* **Deployment name** — a human-readable name for the deployment. This also determines the Kubernetes resource name for the `MCPServer` CR.  
* **OCI image** — pre-filled from the catalog entry's artifact URI. This field cannot be modified in the UI.  
* **Project** — the target namespace. The dropdown lists namespaces accessible to the current user. This should be the gateway namespace (or the team's namespace in a gateway-per-team topology).  
* **YAML configuration** — pre-filled from the catalog's runtime metadata, including port, MCP path, environment variables, and secret references. This is editable — users can modify arguments, add ConfigMap mounts, or adjust environment variables before deploying.

The deployment modal persists data across close and reopen interactions so users can continue without losing progress. Before clicking **Deploy**, ensure that all prerequisites documented in the server's README or catalog description have been satisfied in the target namespace — for example, ServiceAccounts with appropriate RBAC bindings, ConfigMaps with server configuration, or Secrets referenced by environment variables. The deployment will create the `MCPServer` CR, but missing prerequisites will cause the resulting pod to fail or the server to behave incorrectly.

Clicking **Deploy** creates the `MCPServer` CR in the selected namespace. The Lifecycle Operator reconciles it into a running Deployment and Service.

![Deployment modal with pre-filled configuration from catalog metadata][image8]

##### **5.2.2.2 Deploying directly via MCPServer CR**

For servers not in the catalog, or for automated deployments via GitOps pipelines, `MCPServer` CRs can be created directly with `oc apply` or `kubectl apply`. The CR specifies the container image, port, MCP path, arguments, storage mounts, and runtime configuration.

Example `MCPServer` CR for the OpenShift MCP Server:

> [snippets/05-supported-workflows/deploying-directly-via-mcpserver-cr.yaml](snippets/05-supported-workflows/deploying-directly-via-mcpserver-cr.yaml)

```
apiVersion: mcp.x-k8s.io/v1alpha1
kind: MCPServer
metadata:
  name: openshift-mcp-server
spec:
  source:
    type: ContainerImage
    containerImage:
      ref: registry.redhat.io/openshift-mcp-beta/openshift-mcp-server-rhel9:0.2
  config:
    port: 8080
    path: /mcp
    arguments:
      - --config
      - /etc/mcp-config/config.toml
    storage:
      - path: /etc/mcp-config
        permissions: ReadOnly
        source:
          type: ConfigMap
          configMap:
            name: openshift-mcp-server-config
  runtime:
    security:
      serviceAccountName: openshift-mcp-server
```

**Note:** The `openshift-mcp-beta/` image path is a pre-GA registry location. Check the [OpenShift MCP Server repository](https://github.com/openshift/openshift-mcp-server) for the current image reference, as the path and tag may change across releases.

Key fields:

* `source.containerImage.ref` — the OCI image for the MCP server.  
* `config.port` and `config.path` — the port and HTTP path where the server listens for MCP requests.  
* `config.arguments` — command-line arguments passed to the container entrypoint.  
* `config.storage` — mounts ConfigMaps or Secrets into the container (e.g., server configuration files).  
* `runtime.security.serviceAccountName` — the ServiceAccount the server pod runs as. This controls what Kubernetes API access the server has (e.g., a ServiceAccount with the `view` ClusterRole for the OpenShift MCP Server).

Some servers require prerequisite resources before the `MCPServer` CR is applied — for example, a ServiceAccount with appropriate RBAC bindings, or a ConfigMap containing server configuration. These should be created in the same namespace before applying the `MCPServer` CR.

##### **5.2.2.3 Monitoring deployment status**

The **Deployments** tab in the MCP Servers page shows all deployed servers in the selected project. Each entry displays the deployment name, catalog source (if applicable), creation timestamp, and status (Available or Pending). The connection URL — the in-cluster Service URL for the deployed server — is accessible from this view and is needed for gateway registration (section 5.2.3).

![Deployments tab showing server status and connection URL][image9]

##### **5.2.2.4 Managing deployments**

Deployed servers can be edited or deleted through the kebab menu on each deployment entry. Editing updates the `MCPServer` CR, which the Lifecycle Operator reconciles into updated Kubernetes resources. Deleting removes the `MCPServer` CR and its downstream resources.

![Edit and delete options for a deployed MCP server][image10]

#### 5.2.3 Registering MCP Servers with the MCP Gateway

After an MCP server is deployed and running (section 5.2.2), it must be registered with the MCP Gateway so that the broker can discover its tools and route requests to it. Registration requires two resources: an HTTPRoute and an MCPServerRegistration.

##### **5.2.3.1 Create an HTTPRoute**

The HTTPRoute tells the gateway's Envoy proxy how to reach the MCP server. It references the gateway by name and section, and points at the server's Service as a backend.

> [snippets/05-supported-workflows/create-an-httproute.yaml](snippets/05-supported-workflows/create-an-httproute.yaml)

```
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: openshift-mcp-server
spec:
  hostnames:
    - openshift-mcp-server.mcp.local
  parentRefs:
    - name: <gateway-name>
      sectionName: mcps
  rules:
    - backendRefs:
        - name: openshift-mcp-server
          port: 8080
```

Key fields:

* `hostnames` — an internal hostname used for hair-pin routing within the mesh. The convention is `<server-name>.mcp.local`.  
* `parentRefs` — references the team's Gateway CR and its `mcps` listener section. This attaches the route to the gateway's Envoy proxy.  
* `backendRefs` — points at the Kubernetes Service created by the Lifecycle Operator. The name matches the `MCPServer` CR name; the port matches the server's configured port.

Some servers require path matching in the route rules. For example, if the server only handles MCP requests on `/mcp`, add a `matches` block:

> [snippets/05-supported-workflows/create-an-httproute.txt](snippets/05-supported-workflows/create-an-httproute.txt)

```
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /mcp
      backendRefs:
        - name: github-mcp-server
          port: 8080
```

##### **5.2.3.2 Create an MCPServerRegistration**

The MCPServerRegistration tells the MCP Gateway broker about the server. The broker uses this to connect to the server, discover its tools via `tools/list`, and include them in the aggregated tool list served through the gateway.

> [snippets/05-supported-workflows/create-an-mcpserverregistration.yaml](snippets/05-supported-workflows/create-an-mcpserverregistration.yaml)

```
apiVersion: mcp.kuadrant.io/v1alpha1
kind: MCPServerRegistration
metadata:
  name: openshift-mcp-server
spec:
  prefix: openshift_
  targetRef:
    group: gateway.networking.k8s.io
    kind: HTTPRoute
    name: openshift-mcp-server
```

Key fields:

* `prefix` — a string prepended to all tool names from this server. This disambiguates tools when multiple servers expose tools with the same name (e.g., two servers both exposing a `list` tool become `openshift_list` and `github_list`).  
* `targetRef` — references the HTTPRoute created above. The broker resolves this to the server's backend address.

##### **5.2.3.3 Servers that require credentials for tool discovery**

Some MCP servers require authentication even for `tools/list` — for example, the GitHub MCP server needs a valid personal access token to start and respond to any request. For these servers, the MCPServerRegistration includes a `credentialRef` pointing at a Secret containing the credential the broker should use when connecting:

> [snippets/05-supported-workflows/servers-that-require-credentials-for-tool-discovery.yaml](snippets/05-supported-workflows/servers-that-require-credentials-for-tool-discovery.yaml)

```
spec:
  prefix: github_
  targetRef:
    group: gateway.networking.k8s.io
    kind: HTTPRoute
    name: github-mcp-server
  credentialRef:
    name: github-mcp-credential
    key: token
```

The referenced Secret must have the label `mcp.kuadrant.io/secret: "true"` for the broker to pick it up. This credential is used only by the broker for tool discovery — per-user credentials for actual tool calls are handled separately, for example via Vault credential injection (section 5.1.6) or through other secret management approaches.

##### **5.2.3.4 Verification**

Once both resources are applied, the MCP Gateway controller updates the broker's config Secret to include the new server in its upstream list. The broker connects to the server, discovers its tools, and makes them available through the gateway's `/mcp` endpoint. You can verify registration by calling `tools/list` against the gateway URL and confirming the server's tools appear (with the configured prefix).

#### 5.2.4 Creating and Using Virtual MCP Servers

MCPVirtualServers control which tools are visible to users when they call `tools/list` through the gateway. Without MCPVirtualServers, the broker returns every tool from every registered server — which can overwhelm LLMs, increase token consumption, and degrade tool-selection accuracy. MCPVirtualServers let platform or AI engineers define narrow, purpose-specific tool subsets.

##### **5.2.4.1 Creating an MCPVirtualServer**

An MCPVirtualServer lists the specific tools (by their prefixed names) that should be visible:

> [snippets/05-supported-workflows/creating-a-virtualmcpserver.yaml](snippets/05-supported-workflows/creating-a-virtualmcpserver.yaml)

```
apiVersion: mcp.kuadrant.io/v1alpha1
kind: MCPVirtualServer
metadata:
  name: admin-tools
spec:
  description: All OpenShift and utility tools for admins
  tools:
    - openshift_pods_list
    - openshift_pods_get
    - openshift_pods_log
    - openshift_resources_list
    - openshift_resources_get
    - test_greet
    - test_time
```

Tool names must use the prefixed form as configured in the MCPServerRegistration (section 5.2.3) — for example, `openshift_pods_list` rather than `pods_list`. The `description` field is optional but useful for documenting the purpose of the tool subset.

##### **5.2.4.2 How MCPVirtualServer selection works**

The MCPVirtualServer is selected via the `X-Mcp-Virtualserver` header. This header must use the **namespaced format**: `<namespace>/<name>` (e.g., `team-a/admin-tools`). In the automated flow, the gateway-level AuthPolicy injects this header based on JWT group claims — users never set it manually.

For testing or programmatic use, the header can be set explicitly on MCP requests:

> [snippets/05-supported-workflows/how-virtualmcpserver-selection-works.txt](snippets/05-supported-workflows/how-virtualmcpserver-selection-works.txt)

```
X-Mcp-Virtualserver: team-a/admin-tools
```

##### **5.2.4.3 MCPVirtualServers are subtractive only**

An MCPVirtualServer can only filter the set of tools already available through registered MCPServerRegistrations. It cannot add tools that don't exist or grant access to tools the user's AuthPolicy does not permit.

MCPVirtualServers control tool **visibility**, not tool **access** — see section 9.1 for guidance on pairing them with AuthPolicies for enforcement.

#### 5.2.5 Consuming MCP Capabilities in Gen AI Studio / Playground

Once a Playground has been created (section 5.1.7), AI engineers can interact with MCP tools through natural language in the chat interface. Before tools become available, the user must authenticate with the gateway and enable tool use in the Playground UI.

##### **5.2.5.1 Obtaining an auth token**

The MCP Gateway requires a valid JWT for every request. The Playground UI provides an auth token input field where users paste an access token acquired from Keycloak. There are several ways to obtain a token:

* **curl against the Keycloak token endpoint** (suitable for development and testing):

> [snippets/05-supported-workflows/obtaining-an-auth-token.sh](snippets/05-supported-workflows/obtaining-an-auth-token.sh)

```
TOKEN=$(curl -s -X POST \
  https://<keycloak-route>/realms/mcp-gateway/protocol/openid-connect/token \
  -d "grant_type=password&client_id=mcp-playground&username=<user>&password=<pass>&scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

The `scope=openid groups` parameter is required so the JWT includes the `groups` claim, which the gateway uses for MCPVirtualServer routing and group-based authorization. Without it, the user may authenticate successfully but see no tools or receive 403 errors.

* **Browser-based OIDC flow** — in production environments, this would be replaced by an integrated SSO flow where the Dashboard authenticates the user and injects the token automatically.  
* **Device authorization grant** — for headless or CLI-based workflows where a browser is not available.

Once a token is obtained, paste it into the auth token field in the Playground's MCP Gateway configuration panel and click **Authorize**.

![Authorize MCP server dialog with access token input field][image11]

The token has a limited lifetime. If the token expires during a session, tool calls will start returning 401 errors and a fresh token must be pasted.

On successful authorization, the Playground confirms the connection and reports how many tools are available. The **Edit tool selection** link opens a tool picker where individual tools can be enabled or disabled for the session.

![Connection successful dialog showing 5 out of 5 tools active][image12]

##### **5.2.5.2 Enabling tool use in the Playground**

After authorizing, enable the MCP Gateway checkbox in the tool sources panel. This tells Llama Stack/OGX to include tools from the gateway when processing chat messages. When enabled, OGX calls `tools/list` through the gateway (authenticated with the provided token) and makes the returned tools available to the model.

The tools visible to the user are determined by the intersection of their MCPVirtualServer assignment and the wristband-based authorization configured in the gateway AuthPolicy. Both filter the `tools/list` response — the MCPVirtualServer defines which tools are included in the view, and the wristband determines which of those the user is authorized to see. Both are derived from the user's identity in the JWT (sections 5.1.4 and 5.2.4).

The tool selection dialog lists each tool by name and description, allowing users to fine-tune which tools the model can invoke. This is useful when a user has access to many tools but wants to focus the model on a specific task.

![Tool selection dialog showing available tools with names and descriptions][image13]

##### **5.2.5.3 Interacting with tools**

With the gateway enabled, the user can type natural language messages in the chat box. The model decides when to invoke tools based on the user's message and the available tool descriptions. Tool calls are routed through Llama Stack/OGX to the gateway, which handles authentication, authorization, credential injection, and routing to the appropriate MCP server — all transparently.

Tool results are returned to the model, which incorporates them into its response. The user sees the final assembled response in the chat interface, with structured tool output rendered inline.

![Playground showing a tool call result with structured pod information][image14]

#### 5.2.6 Bringing Your Own MCP Server

To make a custom MCP server discoverable in the MCP Catalog, create an `mcp-catalog-sources` ConfigMap in the `rhoai-model-registries` namespace. This ConfigMap has two parts: a `sources.yaml` key that defines the catalog index, and a second key containing the actual server definitions.

##### **5.2.6.1 ConfigMap structure**

The ConfigMap requires two data keys:

* `sources.yaml` — the catalog index. Each entry must use `type: yaml` with a `yamlCatalogPath` pointing to the second key's mount path. Inline entries (`type: inline`) are not supported.  
* A catalog data key (e.g., `custom-mcp-servers.yaml`) — the server definitions in YAML format, following the same schema as built-in catalogs.

> [snippets/05-supported-workflows/configmap-structure.yaml](snippets/05-supported-workflows/configmap-structure.yaml)

```
apiVersion: v1
kind: ConfigMap
metadata:
  name: mcp-catalog-sources
  namespace: rhoai-model-registries
data:
  sources.yaml: |
    mcp_catalogs:
      - name: Custom MCP Servers
        id: custom_mcp_servers
        type: yaml
        enabled: true
        properties:
          yamlCatalogPath: /data/user-mcp-sources/custom-mcp-servers.yaml
        labels:
          - Custom
    labels:
      - name: Custom
        assetType: mcp_servers
        displayName: Custom MCP servers
        description: Team-curated MCP servers.
  custom-mcp-servers.yaml: |
    source: Custom MCP
    mcp_servers:
      - name: my-custom-server
        provider: My Team
        license: apache-2.0
        description: A custom MCP server with domain-specific tools.
        version: "1.0"
        transports:
          - http
        tools:
          - name: my_tool
            description: Does something useful
            parameters:
              - name: input
                type: string
                description: The input value
                required: true
        artifacts:
          - uri: oci://registry.example.com/my-custom-server:1.0
        runtimeMetadata:
          defaultPort: 8080
          mcpPath: /mcp
          defaultArgs:
            - --http
            - 0.0.0.0:8080
```

**Note:** The namespace is `rhoai-model-registries` for RHOAI deployments. Open Data Hub (ODH) deployments use `odh-model-registries` instead.

##### **5.2.6.2 Server entry fields**

Each server entry in the catalog data key must include:

* `name` — unique identifier for the server.  
* `provider` — who maintains the server.  
* `description` — what the server does.  
* `version` — server version string.  
* `transports` — must include `http`.  
* `tools` — list of tools with `name`, `description`, and `parameters` (each with `name`, `type`, `description`, `required`).  
* `artifacts` — list with at least one entry containing `uri` (the OCI image URI, prefixed with `oci://`).  
* `runtimeMetadata` — `defaultPort`, `mcpPath`, and `defaultArgs`. These pre-fill the deployment modal when a user deploys from the Catalog UI.

Optional fields include `license`, `license_link`, and `repositoryUrl`.

##### **5.2.6.3 Applying and verifying**

After applying the ConfigMap, restart the `model-catalog` deployment to ensure the new entries are indexed immediately (the catalog may also pick up ConfigMap changes automatically after a delay):

> [snippets/05-supported-workflows/applying-and-verifying.sh](snippets/05-supported-workflows/applying-and-verifying.sh)

```
oc apply -f mcp-catalog-sources.yaml
oc rollout restart deployment model-catalog -n rhoai-model-registries
oc rollout status deployment model-catalog -n rhoai-model-registries --timeout=60s
```

Once the rollout completes, open the MCP Catalog in the RHOAI Dashboard. The custom servers appear under their own label (e.g., "Custom MCP servers") alongside the built-in catalog entries. Clicking into a server shows its metadata, tools, and the **Deploy MCP server** button — following the same deployment workflow as built-in servers (section 5.2.2).

![Custom MCP servers appearing in the RHOAI MCP Catalog alongside built-in entries][image15]

To verify programmatically, query the Catalog API:

> [snippets/05-supported-workflows/applying-and-verifying-2.sh](snippets/05-supported-workflows/applying-and-verifying-2.sh)

```
TOKEN=$(oc create token model-catalog -n rhoai-model-registries)
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://<model-catalog-route>/api/mcp_catalog/v1alpha1/mcp_servers"
```

##### **5.2.6.4 Adding servers to an existing ConfigMap**

To add a new server to an existing `mcp-catalog-sources` ConfigMap, append the server entry to the `mcp_servers` list in the catalog data key and re-apply. Restart `model-catalog` after each update. The server packaging requirements in section 8.1 and conventions in section 8.2 apply to any custom server registered through the catalog.

