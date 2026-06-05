# MCP Ecosystem Demo Agent

An OpenShift cluster operations agent that connects to MCP servers
through the MCP Gateway, demonstrating identity-based tool access
control with Keycloak authentication and wristband tool filtering.

## What This Agent Does

This agent uses the MCP Ecosystem on Red Hat OpenShift AI to interact
with cluster resources. It connects to the MCP Gateway with a Keycloak
JWT, receives a filtered set of tools based on its identity, and
responds to natural language queries about cluster state.

- **Admin identity** (mcp-admins group): 14 OpenShift management tools
- **User identity** (mcp-users group): 8 read-only tools
- Same gateway, same MCP server — different identity, different access

The agent is part of the
[MCP Ecosystem Workshop](https://rdwj.github.io/workshop-setup-mcp/),
where students deploy and configure the full MCP stack, then use this
agent to verify everything works end-to-end.

## Quickstart

```sh
make install       # Create .venv, install dependencies
make run-local     # Start HTTP server on port 8080
```

Test it:

```sh
curl -s http://localhost:8080/healthz
curl -s http://localhost:8080/v1/agent-info | python -m json.tool
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}]}'
```

## Gateway Auth Demo

The `scripts/demo.sh` script walks through the full gateway auth flow:

1. **Unauthenticated request** — gateway returns 401
2. **Obtain JWT** from Keycloak (`client_credentials` grant)
3. **Initialize MCP session** with the JWT (gets session ID)
4. **List tools** — admin identity sees all 14 tools
5. **Call a tool** — `openshift_namespaces_list` returns live cluster data
6. **User identity comparison** — user identity sees only 8 read-only tools

```sh
# Required exports (see scripts/setup-keycloak.sh output)
export KEYCLOAK_URL=https://keycloak-keycloak.apps.<cluster>
export CLIENT_SECRET=<admin-client-secret>
export MCP_GATEWAY_URL=https://<gateway-route>/mcp

# Optional: user-level identity for step 6
export USER_CLIENT_SECRET=<user-client-secret>

./scripts/demo.sh
```

The setup script (`scripts/setup-keycloak.sh`) creates both Keycloak
clients. After running it, assign their service accounts to groups in the
Keycloak admin console:

- `service-account-mcp-gateway` -> `mcp-admins` (14 tools)
- `service-account-mcp-user-agent` -> `mcp-users` (8 read-only tools)

## Container Deployment

When deployed in OpenShift, the agent automatically acquires an OAuth
token at startup if Keycloak credentials are configured.

The container's startup wrapper (`scripts/start-with-auth.sh`) handles
the OAuth flow:

1. Checks if `KEYCLOAK_URL`, `KEYCLOAK_CLIENT_ID`, and `KEYCLOAK_CLIENT_SECRET` are set
2. Makes a `client_credentials` grant request to Keycloak
3. Exports the JWT as `MCP_AUTH_TOKEN` environment variable
4. Starts the agent, which reads the token and includes it in MCP Gateway requests

If the token expires mid-session, the `mcp_auth_refresh` lifecycle hook
re-acquires a fresh token and reconnects automatically.

**Configuration** (in `chart/values.yaml`):

```yaml
config:
  KEYCLOAK_URL: https://keycloak.example.com
  KEYCLOAK_REALM: mcp-gateway
  KEYCLOAK_CLIENT_ID: workshop-agent
  KEYCLOAK_CLIENT_SECRET: <secret>
  MCP_GATEWAY_URL: http://mcp-gateway.mcp-system.svc.cluster.local:8080/mcp
```

For production, use a Secret for credentials:

```yaml
env:
  - name: KEYCLOAK_CLIENT_SECRET
    valueFrom:
      secretKeyRef:
        name: keycloak-credentials
        key: client-secret
```

See [`docs/oauth-setup.md`](docs/oauth-setup.md) for detailed setup and
troubleshooting.

## Project Structure

| Path | Purpose |
|------|---------|
| `src/agent.py` | Agent subclass — your main logic |
| `tools/` | One `@tool`-decorated file per tool |
| `prompts/` | Markdown + YAML frontmatter, one per prompt |
| `hooks/` | Lifecycle hooks (token acquisition, refresh) |
| `skills/` | agentskills.io directories (lazy-loaded) |
| `rules/` | Plain Markdown constraints (loaded at startup) |
| `agent.yaml` | Configuration with `${VAR:-default}` substitution |
| `chart/` | Helm chart for OpenShift deployment |
| `scripts/` | Demo and setup scripts |

## Configuration

All configuration lives in `agent.yaml`. Every value supports `${VAR:-default}`
environment variable substitution — same file works for local dev and production.

| Section | Key env vars | Purpose |
|---------|-------------|---------|
| `agent` | `AGENT_NAME` | Name, description, version |
| `model` | `MODEL_ENDPOINT`, `MODEL_NAME` | LLM endpoint (OpenAI-compatible) |
| `mcp_servers` | `MCP_GATEWAY_URL` | MCP Gateway connection with auth headers |
| `prompts` | — | Prompt directory and system prompt designation |
| `server` | `HOST`, `PORT` | HTTP server binding |

In production, override values via ConfigMap env vars — the image is immutable.

## Deployment

```sh
make build                          # Build container (podman, linux/amd64)
podman push $IMAGE quay.io/...      # Push to registry
make deploy PROJECT=my-namespace    # Deploy via Helm
make redeploy PROJECT=my-namespace  # Force-redeploy (fresh image pull)
make clean PROJECT=my-namespace     # Remove from OpenShift
```

## Development Commands

```sh
make test          # Run pytest
make test-cov      # Run pytest with coverage
make eval          # Run eval cases (mock LLM)
make lint          # Run ruff linter
make vendor        # Vendor fipsagents source (replaces PyPI dep)
make help          # Show all targets
```

## Framework

Built on [fipsagents](https://github.com/fips-agents/agent-template)
(`BaseAgent`). Scaffolded with `fips-agents create agent`.
