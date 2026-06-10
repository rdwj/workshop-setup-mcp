## **8\. Bring Your Own MCP Server**

Bringing a custom API or MCP server into the RHOAI MCP ecosystem requires packaging the server as a container image, validating it against platform requirements, authoring catalog metadata, and registering it with the gateway. Today this is a manual process — each step is documented below. Tracking items [RHAIRFE-2081](https://redhat.atlassian.net/browse/RHAIRFE-2081) and [RHAIRFE-2082](https://redhat.atlassian.net/browse/RHAIRFE-2082) cover potential future automation of parts of this workflow.

### **8.1 Packaging and Validating the Server**

Before a server can be deployed and registered, it must be packaged as a compliant container image and validated against the platform's protocol, security, and operator requirements. The checklist below is organized into three areas.

#### 8.1.1 Container Packaging

* **Base image:** Use a UBI (Universal Base Image) or equivalent Red Hat-supported base image for compatibility with OpenShift's image policies.  
* **Explicit HTTP transport:** Many MCP server frameworks default to stdio transport. Always explicitly configure HTTP transport via command-line arguments (e.g., `--http 0.0.0.0:8080`) or environment variables. Servers that default to stdio will start, produce no error output, and exit silently — making the failure difficult to diagnose from pod logs alone.  
* **Bind to `0.0.0.0`:** The server must listen on all interfaces. Servers that bind to `127.0.0.1` are unreachable from the Envoy proxy and will appear healthy but fail to handle requests from the gateway.  
* **Accept credentials via HTTP headers:** Servers that need downstream credentials (API keys, tokens) should accept them via HTTP headers, not only through environment variables. Header-based credentials enable per-user credential injection through the gateway (section 5.1.6); environment-variable-only servers require a 1:1 deployment-per-user model.  
* **Declare a stable MCP path:** The server must serve MCP requests at a predictable path (e.g., `/mcp`, `/sse`). This path is configured in the `MCPServer` CR (`spec.config.path`) and referenced by the HTTPRoute during gateway registration.  
* **Expose a health endpoint:** Provide a health check path (e.g., `/healthz` or the MCP path itself) and configure liveness and readiness probes in the `MCPServer` CR or Deployment spec for proper Kubernetes lifecycle management.  
* **Stateless where possible:** Stateless servers are easier to scale horizontally and recover from pod restarts. If the server requires persistent state, document the storage requirements so they can be declared in catalog metadata (`runtimeMetadata.capabilities`).

#### 8.1.2 Protocol and Security Verification

| Check | Detail |
| :---- | :---- |
| **HTTP Streamable or SSE** | Stdio-only servers cannot be routed through the gateway, shared across clients, or secured with AuthPolicies. Verify the server communicates over HTTP. |
| **Tool discovery** | The server must respond to `tools/list` so the gateway broker can discover and advertise available tools. Verify `tools/call` executes correctly for each advertised tool. |
| **OCI image** | The server must be published as a pullable OCI container image. |
| **Vulnerability scan** | Scan the image with Clair (or an equivalent scanner) and resolve any impacting CVE vulnerabilities before deploying. |
| **SAST** | If source code is available, run static analysis (e.g., Snyk) against it. |

#### 8.1.3 OpenShift and Lifecycle Operator Compatibility

**OpenShift restricted SCC compliance.** All containers on OpenShift run under the restricted Security Context Constraint by default. The server image must satisfy every constraint below; non-compliant images will fail to schedule or will be killed at startup.

| Constraint | Requirement |
| :---- | :---- |
| Non-root user | `runAsNonRoot: true` — the container process must not run as UID 0\. |
| Drop capabilities | `capabilities: { drop: ["ALL"] }` — no Linux capabilities retained. |
| No privilege escalation | `allowPrivilegeEscalation: false` |
| No host access | Must not require host networking, host PID, or privileged mode. |
| Read-only root filesystem | Set `readOnlyRootFilesystem: true` where possible. If the server writes to the filesystem, use `emptyDir` or PVC mounts for writable paths. |
| Base image | UBI or Red Hat-supported base image (see Container Packaging above). |

Reference: [OpenShift SCC documentation](https://docs.openshift.com/container-platform/latest/authentication/managing-security-context-constraints.html)

**Lifecycle Operator compatibility.** Validate that the server deploys successfully via the `MCPServer` CR and that the lifecycle operator creates a healthy Deployment and Service. Beyond a successful deployment, verify that all of the server's configuration options can be expressed through the `MCPServer` CRD:

| CRD field | What it covers |
| :---- | :---- |
| `spec.source.containerImage.ref` | Container image reference |
| `spec.config.port` | Listen port |
| `spec.config.path` | MCP HTTP path (e.g., `/mcp`) |
| `spec.config.arguments` | Command-line arguments (maps to container `args`) |
| `spec.config.env[]` | Environment variables (`name` / `value` pairs) |
| `spec.config.storage[]` | ConfigMap or Secret mounts (`path`, `permissions`, `source.type`, `source.configMap.name`) |
| `spec.runtime.replicas` | Replica count |
| `spec.runtime.security.serviceAccountName` | ServiceAccount for the server pod |
| `spec.runtime.security.securityContext` | Pod-level security context overrides |

If the server appears to require configuration mechanisms not directly supported by the CRD — runtime-generated config files, init containers, sidecar processes — first evaluate whether the same outcome can be achieved through one of the supported mechanisms (e.g., pre-generating the config file into a ConfigMap and mounting it via `config.storage`, or moving initialization logic into the entrypoint and controlling it via `config.arguments` or `config.env`). Only if none of the CRD-supported options can cover the requirement should a raw Deployment \+ Service be used instead of the `MCPServer` CR.

**Mapping validation results to catalog metadata.** The checks above feed directly into the `securityIndicators` fields in the catalog entry (section 8.2):

| Validation outcome | Catalog field | Value |
| :---- | :---- | :---- |
| Source code reviewed / verified | `verifiedSource` | `true` |
| SAST scan passed | `sast` | `true` |
| Server uses secure (TLS) endpoints | `secureEndpoint` | `true` |
| All tools are read-only | `readOnlyTools` | `true` |

### **8.2 Preparing Catalog Metadata**

The MCP Catalog discovers and presents servers through structured YAML metadata. This metadata is what makes a server visible in the Catalog UI and drives the deployment modal's pre-filled values. Author one entry per server following the [catalog YAML specification](https://github.com/kubeflow/hub/blob/main/docs/catalog-yaml-reference.md); working examples are available in the [model-metadata-collection repository](https://github.com/opendatahub-io/model-metadata-collection/tree/main/input/mcp_servers).

#### 8.2.1 Required and Recommended Fields

| Field | Purpose |
| :---- | :---- |
| `name` | Unique server identifier (used as the key in the catalog). |
| `provider` | Organization or team that maintains the server. |
| `description` | Short summary displayed in the catalog card. |
| `version` | Semantic version (e.g., `"1.0.0"`). |
| `transports` | Supported transport protocols — typically `["http"]` or `["sse"]`. |
| `artifacts` | Container image URIs (for `local` deployment mode). Each entry includes a `uri` field (e.g., `oci://registry.example.com/org/server:1.0.0`). |
| `deploymentMode` | `local` for container-deployed servers (default), `remote` for externally hosted services. Remote servers use `endpoints` instead of `artifacts`. |

#### 8.2.2 Tool Definitions

Every tool the server exposes should be declared under `tools`. Each entry requires:

> [snippets/08-bring-your-own-mcp-server/tool-definitions.txt](snippets/08-bring-your-own-mcp-server/tool-definitions.txt)

```
tools:
  - name: list_resources
    description: List platform resources
    accessType: read_only      # read_only | read_write | execute
    parameters:
      - name: namespace
        type: string
        description: Target namespace
        required: false
```

`accessType` is required per tool and determines how the catalog classifies the tool's impact. It also feeds into whether `securityIndicators.readOnlyTools` can be set to `true` (only if every tool is `read_only`).

#### 8.2.3 Runtime Metadata

`runtimeMetadata` tells the deployment modal how to pre-fill the `MCPServer` CR and what prerequisites the server needs:

> [snippets/08-bring-your-own-mcp-server/runtime-metadata.txt](snippets/08-bring-your-own-mcp-server/runtime-metadata.txt)

```
runtimeMetadata:
  defaultPort: 8080
  mcpPath: /mcp
  defaultArgs:
    - --config
    - /etc/mcp-config/config.toml
  prerequisites:
    serviceAccount:
      required: true
      hint: "Needs 'edit' ClusterRole for namespace access"
      suggestedName: my-server-sa
    secrets:
      - name: api-credentials
        description: "API key for downstream service"
        keys:
          - key: api-token
            description: API token value
            envVarName: API_TOKEN
            required: true
        mountAsFile: false
    configMaps:
      - name: server-config
        description: "Server configuration file"
        mountAsFile: true
        mountPath: /etc/mcp-config
        keys:
          - key: config.toml
            description: TOML configuration
            required: true
    environmentVariables:
      - name: LOG_LEVEL
        description: Logging verbosity
        required: false
        type: string
  recommendedResources:
    minimal:
      cpu: "100m"
      memory: "128Mi"
    recommended:
      cpu: "500m"
      memory: "512Mi"
  healthEndpoints:
    liveness: /healthz
    readiness: /healthz
  capabilities:
    requiresNetwork: true
    requiresFileSystem: false
    requiresGPU: false
```

#### 8.2.4 Security Indicators

Set these based on the validation results from section 8.1:

> [snippets/08-bring-your-own-mcp-server/security-indicators.txt](snippets/08-bring-your-own-mcp-server/security-indicators.txt)

```
securityIndicators:
  verifiedSource: true
  secureEndpoint: true
  sast: true
  readOnlyTools: false   # true only if every tool has accessType: read_only
```

#### 8.2.5 Optional Enrichment

Additional fields improve catalog discoverability and documentation:

| Field | Purpose |
| :---- | :---- |
| `readme` | Full Markdown documentation rendered in the catalog detail view. |
| `logo` | Data URI or URL for the server's icon. |
| `tags` | Searchable tags (e.g., `["kubernetes", "monitoring"]`). |
| `documentationUrl` | Link to external documentation. |
| `repositoryUrl` | Link to the source repository. |
| `license` | SPDX license identifier (e.g., `apache-2.0`). |

### **8.3 Publishing to the MCP Catalog**

To make the server discoverable in the Catalog UI, create a `mcp-catalog-sources` ConfigMap containing the metadata authored in section 8.2. The full procedure — ConfigMap structure, namespace placement, and verification steps — is documented in section 5.2.6.

After applying the ConfigMap, verify that the server appears in the Catalog and that the deployment modal pre-fills correctly from `runtimeMetadata` (port, MCP path, arguments, environment variables, secret references).

### **8.4 Deploying, Registering, and Making Available**

Once the server is published to the catalog, deploy it into the ecosystem using the same workflows described in section 5:

1. **Deploy the server** via `MCPServer` CR (section 5.2.2). For servers whose configuration cannot be fully expressed through the CRD (see section 8.1), deploy using a standard Deployment \+ Service instead.  
2. **Register with the gateway** by creating an HTTPRoute and MCPServerRegistration (section 5.2.3).  
3. **Configure access control** if needed — create or update AuthPolicies for per-user credential injection or group-based restrictions (sections 5.1.4–5.1.6), and assign the server's tools to the appropriate VirtualMCPServers.

