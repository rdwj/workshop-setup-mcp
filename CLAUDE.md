# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

A hands-on workshop for deploying the MCP Ecosystem on Red Hat OpenShift AI. The repo contains:

- **Workshop modules** (`deploy/workshop/`) — step-by-step guides for deploying the MCP Gateway, MCP servers, identity/auth, and an agent
- **Platform base** (`deploy/base/`) — Kustomize overlay for cluster prerequisites (RHOAI, NFD, Authorino)
- **Demo stack** (`demo/`) — three components that form the end-to-end demo:
  - `demo/agent/` — Python/fipsagents AI agent with MCP tool integration
  - `demo/gateway/` — Go HTTP gateway (auth, file upload, routing)
  - `demo/ui/` — Go chat UI with streaming support
The agent itself is minimal (`demo/agent/src/agent.py` is ~30 lines). Most complexity lives in the authentication flow, the workshop deployment stages, and the framework (vendored, do not edit).

## Build and Test Commands

Agent commands (run from `demo/agent/`):

```bash
make install                         # Create .venv, install deps
make run-local                       # Start HTTP server on port 8080
make test                            # Run pytest
make test-cov                        # pytest with coverage
make lint                            # Lint with ruff
make build                           # Build container (podman, linux/amd64)
make deploy PROJECT=<ns>             # Deploy to OpenShift via Helm
make redeploy PROJECT=<ns> IMAGE_TAG=<tag>  # Force fresh image pull
make eval                            # Run all eval cases
make clean PROJECT=<ns>              # Remove from OpenShift
```

Gateway and UI commands (run from `demo/gateway/` or `demo/ui/`):

```bash
make build-openshift PROJECT=<ns>    # Create BuildConfig + build on cluster
make deploy PROJECT=<ns>             # Deploy via Helm
```

## Architecture

### Agent Lifecycle

Each HTTP request to `/v1/chat/completions` creates a fresh `MyAgent` instance. The lifecycle is: `setup()` (load config, connect MCP, acquire token) -> `step()` loop (call LLM, dispatch tools) -> `shutdown()`. Responses stream via SSE.

### MCP Gateway Authentication Flow

1. **Hook fires** (`hooks/acquire-token.yaml`): Before MCP connection, `hooks/acquire-token.sh` performs a Keycloak `client_credentials` grant
2. **Token injected**: The JWT is set as `MCP_AUTH_TOKEN` env var
3. **Gateway connection**: `agent.yaml` passes `Authorization: Bearer ${MCP_AUTH_TOKEN}` in MCP server headers
4. **Tool filtering**: The gateway returns only the tools the authenticated identity is allowed to use
5. **Refresh**: On auth failure, `hooks/refresh-token.yaml` re-acquires a fresh token

Key env vars for auth:
- `KEYCLOAK_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET` -- Keycloak OAuth
- `MCP_GATEWAY_URL` -- MCP Gateway endpoint
- `MCP_AUTH_TOKEN` -- JWT (managed automatically by hooks)

### Workshop Deployment Stages

The `deploy/` directory contains Kustomize overlays that build up the full stack in order:

| Stage | Directory | Purpose |
|-------|-----------|---------|
| 00 | model-endpoint | LLM endpoint (vLLM on OpenShift AI) |
| 01 | gateway-infrastructure | Kuadrant API gateway |
| 02 | mcp-gateway | MCP Gateway broker |
| 03 | mcp-server-prerequisites | ServiceAccount, RBAC, ConfigMap for MCP server |
| 04 | mcp-server | MCP server deployment |
| 05 | gateway-registration | Register MCP servers with gateway |
| 06 | identity-auth | Keycloak realm, clients, user groups |
| 07 | deploy-agent | Build and deploy the agent, gateway, and UI |
| 08 | agent-test | Agent testing (admin + user configs) |
| 09 | vault | HashiCorp Vault integration |
| 10 | external-model | External model endpoint |

`deploy/base/` contains OpenShift operator subscriptions (RHOAI, NFD, Authorino, Web Terminal, GPU).

### Two-Plane Tool System

Tools declare visibility: `llm_only` (LLM decides when to call), `agent_only` (agent code via `self.use_tool()`), or `both`. One file per tool in `tools/`, auto-discovered at startup. MCP-discovered tools default to `llm_only`.

### Prompt Assembly Layers

When `prompt_assembly:` is enabled in `agent.yaml` (default), the system prompt is built from four layers in precedence order: Identity (`identity.md`) -> Personality (`personality.md`, off by default) -> Governance (`rules/`) -> Capabilities (`skills/`). Set `LOG_LEVEL=DEBUG` to see the assembly audit log.

## Key Files

| File | Purpose |
|------|---------|
| `demo/agent/src/agent.py` | Agent subclass -- your code goes here |
| `demo/agent/src/token_manager.py` | OAuth token management |
| `demo/agent/src/fipsagents/` | Vendored framework -- do not edit |
| `demo/agent/tools/check_auth.py` | Gateway connectivity/auth verification tool |
| `demo/agent/hooks/acquire-token.sh` | Keycloak OAuth token acquisition script |
| `demo/agent/scripts/start-with-auth.sh` | Container entrypoint (acquires token, starts agent) |
| `demo/agent/agent.yaml` | All config with `${VAR:-default}` env var substitution |
| `demo/agent/identity.md` | Agent identity (who it is) |
| `demo/agent/prompts/system.md` | System prompt with variable substitution |

## Agent Development Patterns

The agent subclass implements `step()`. Common patterns:

```python
# Basic: call LLM, dispatch any tool calls, return
response = await self.call_model()
response = await self.run_tool_calls(response)
return StepResult.done(result=response.content)

# Structured output (returns Pydantic instance)
result = await self.call_model_json(MySchema, messages=[...])

# Agent-code tool call (plane 1, invisible to LLM)
result = await self.use_tool("check_gateway_auth")

# Validation with retry
text = await self.call_model_validated(my_validator_fn, max_retries=3)
```

## Common Mistakes

- **Do not edit `src/fipsagents/`** -- it is the vendored framework.
- **Do not import `openai` directly** -- use BaseAgent's `call_model*` methods (the project uses `litellm`).
- **Do not hardcode model names or endpoints** -- use `agent.yaml` with `${VAR:-default}`.
- **Do not skip `visibility` on tools** -- every tool must declare `llm_only`, `agent_only`, or `both`.
- **Do not omit `tool_call_id` when appending tool results** in manual tool-call loops.
- **Do not confuse `self.use_tool()` with `self.tools.execute()`** -- `use_tool()` is for agent-code calls (plane 1); `tools.execute()` is for the LLM tool-call dispatch loop (plane 2).

## Slash Command Workflow

Scaffolding pipeline (run in order): `/plan-agent` -> `/create-agent` -> `/exercise-agent` -> `/deploy-agent`

Extension commands (run any time after `/create-agent`): `/add-tool`, `/add-skill`, `/add-memory`

Iterative loop: add capability -> `/exercise-agent` -> `/deploy-agent`

## Dependencies

- **litellm** (>=1.83.0) -- Multi-provider LLM adapter (do NOT use 1.82.7 or 1.82.8, supply chain compromise)
- **fastmcp** (v3) -- MCP client (streamable-http transport)
- **fastapi** + **uvicorn** -- HTTP server
- **pydantic** -- Config validation and structured output
- **memoryhub** (optional) -- Memory backend SDK
