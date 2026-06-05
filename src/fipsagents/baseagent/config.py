"""Agent configuration with YAML parsing and environment variable substitution.

Loads ``agent.yaml``, resolves ``${VAR:-default}`` placeholders against the
current environment, and validates the result into typed Pydantic models.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Union

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""


# ---------------------------------------------------------------------------
# Environment variable substitution
# ---------------------------------------------------------------------------

# Matches ${VAR}, ${VAR:-default}, or ${VAR-default}
_ENV_PATTERN = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::?-(?P<default>[^}]*))?\}"
)


def substitute_env_vars(
    value: str,
    *,
    env: dict[str, str] | None = None,
    strict: bool = False,
) -> str:
    """Replace ``${VAR:-default}`` tokens in *value* with environment values.

    Parameters
    ----------
    value:
        The string that may contain ``${VAR:-default}`` placeholders.
    env:
        Environment mapping.  Defaults to ``os.environ``.
    strict:
        When *True*, raise ``ConfigError`` for any variable that has neither
        an environment value nor a default.  When *False* (the default), the
        raw placeholder is left in place so it surfaces clearly in logs.
    """
    env = env if env is not None else os.environ

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        result = env.get(name)
        if result is not None:
            return result
        if default is not None:
            return default
        if strict:
            raise ConfigError(
                f"Environment variable ${{{name}}} is required but not set "
                f"and has no default value"
            )
        return match.group(0)  # leave placeholder intact

    return _ENV_PATTERN.sub(_replace, value)


def _substitute_recursive(
    obj: Any,
    *,
    env: dict[str, str] | None = None,
    strict: bool = False,
) -> Any:
    """Walk an arbitrary structure and substitute env vars in all strings."""
    if isinstance(obj, str):
        return substitute_env_vars(obj, env=env, strict=strict)
    if isinstance(obj, dict):
        return {
            k: _substitute_recursive(v, env=env, strict=strict)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_substitute_recursive(v, env=env, strict=strict) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Adapter sidecar constants
# ---------------------------------------------------------------------------

_ADAPTER_PORT: int = 8081
_ADAPTER_ENDPOINT: str = f"http://localhost:{_ADAPTER_PORT}/v1"
_OFF_PLATFORM_PROVIDERS: frozenset[str] = frozenset({"anthropic", "bedrock", "azure"})


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class LimitsConfig(BaseModel):
    """Per-turn resource limits checked inside astep_stream().

    All fields are optional. None means no limit (backward compatible).
    """

    max_tokens_per_turn: int | None = None
    max_iterations_per_turn: int | None = None
    max_cost_per_turn_usd: float | None = None


class LLMConfig(BaseModel):
    """LLM provider and generation settings."""

    provider: Literal["openai", "anthropic", "bedrock", "azure"] = "openai"
    endpoint: str | None = None
    name: str = "meta-llama/Llama-3.3-70B-Instruct"
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)


class McpServerConfig(BaseModel, extra="forbid"):
    """Connection details for a single MCP server.

    Exactly one transport must be specified:

    - **HTTP** (streamable-http): set ``url``.
    - **stdio** (subprocess): set ``command`` (and optionally ``args``,
      ``env``, ``cwd``).

    Unknown fields are rejected (``extra="forbid"``) so that typos like
    ``transport: streamable-http`` fail loudly instead of being silently
    ignored.
    """

    # HTTP transport
    url: str | None = None
    headers: dict[str, str] | None = None
    _header_templates: dict[str, str] | None = PrivateAttr(default=None)

    # stdio transport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None

    @model_validator(mode="after")
    def _require_exactly_one_transport(self) -> "McpServerConfig":
        has_url = self.url is not None
        has_command = self.command is not None
        if not has_url and not has_command:
            raise ValueError(
                "McpServerConfig requires either 'url' (HTTP) or "
                "'command' (stdio), got neither"
            )
        if has_url and has_command:
            raise ValueError(
                "McpServerConfig cannot have both 'url' and 'command' "
                "— pick one transport"
            )
        return self


class PlatformMcpServer(BaseModel):
    """A single MCP server registered with OGX (LlamaStack) for server-side
    orchestration.

    Two reference modes are supported, matching the OGX Responses API
    ``tools`` array shape:

    - **Connector reference**: ``name`` plus ``connector_id``.  Serialized
      as ``{"type":"mcp","server_label":<name>,"connector_id":<id>}``.
      The connector must be pre-registered in OGX's stack YAML
      (``connectors:`` block).  Right when the platform team controls
      MCP wiring centrally.
    - **Inline URL**: ``name`` plus ``url``.  Serialized as
      ``{"type":"mcp","server_label":<name>,"server_url":<url>}``.  Right
      when the platform team has not pre-registered the server.

    ``name`` is always required and becomes ``server_label`` on the wire
    — a human-readable identifier OGX surfaces in logs and traces.
    ``authorization`` is an optional bearer token (sent without the
    ``Bearer `` prefix) for OAuth-protected MCP servers.

    Exactly one of ``connector_id`` / ``url`` must be set.
    """

    name: str
    connector_id: str | None = None
    url: str | None = None
    authorization: str | None = None

    @field_validator("name")
    @classmethod
    def _name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("PlatformMcpServer.name must not be empty")
        return v

    @model_validator(mode="after")
    def _require_one_reference(self) -> "PlatformMcpServer":
        has_connector = bool(self.connector_id and self.connector_id.strip())
        has_url = bool(self.url and self.url.strip())
        if not has_connector and not has_url:
            raise ValueError(
                "PlatformMcpServer requires either 'connector_id' (registered "
                "connector) or 'url' (inline server URL), got neither"
            )
        if has_connector and has_url:
            raise ValueError(
                "PlatformMcpServer cannot have both 'connector_id' and 'url' "
                "— pick one reference mode"
            )
        return self


class ModerationConfig(BaseModel):
    """Pre/post moderation classification via OGX's ``/v1/moderations``.

    Separate from shield enforcement — this is observability-only.  When
    ``enabled`` is True, the framework calls
    ``client.moderations.create()`` on the user message before the
    Responses call and on the assistant content after, emitting structured
    log events.  No prompt is ever blocked here; that is what
    ``platform.guardrails`` (shields) are for.

    ``categories`` is the explicit list of classification categories to
    request from OGX.  Empty list (the default) means "all default
    categories the moderation model exposes".
    """

    enabled: bool = False
    categories: list[str] = Field(default_factory=list)


class PlatformConfig(BaseModel):
    """Opt-in delegation of LLM orchestration to OGX (LlamaStack rebrand).

    When ``enabled`` is True, the agent talks to OGX's ``/v1/responses``
    endpoint via ``client.responses.create()`` instead of
    ``chat.completions.create()``.  MCP tool calls, shield enforcement,
    tool-result feeding, and the inference loop all happen server-side
    inside OGX.

    When ``enabled`` is False (the default), this block is inert and the
    agent uses the standard chat-completions path with client-side MCP
    orchestration via ``mcp_servers`` — fully backward-compatible.

    ``endpoint`` is the OGX base URL (typically ending in ``/v1``).
    Required when ``enabled`` is True.

    ``mcp`` lists MCP servers OGX should orchestrate on the agent's
    behalf.  Replaces the top-level ``mcp_servers:`` block when platform
    mode is on; the framework skips its own ``connect_mcp()`` startup
    loop in that case.

    ``guardrails`` is a list of shield IDs registered in OGX's
    ``config.yaml`` (Llama Guard, Prompt Guard, etc.).  Passed as the
    ``guardrails`` array on every Responses request.  Empty list means
    no enforcement.

    ``moderation`` is the observability-only classifier — see
    :class:`ModerationConfig`.
    """

    enabled: bool = False
    endpoint: str | None = None
    mcp: list[PlatformMcpServer] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    moderation: ModerationConfig = Field(default_factory=ModerationConfig)

    @field_validator("enabled", mode="before")
    @classmethod
    def _coerce_enabled(cls, v: Any) -> Any:
        """Coerce string values from env-var substitution (``${PLATFORM_MODE:-false}``)."""
        if isinstance(v, str):
            lowered = v.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off", ""}:
                return False
            raise ValueError(
                f"platform.enabled must be a boolean, got '{v}'"
            )
        return v

    @model_validator(mode="after")
    def _require_endpoint_when_enabled(self) -> "PlatformConfig":
        if self.enabled and not (self.endpoint and self.endpoint.strip()):
            raise ValueError(
                "platform.endpoint is required when platform.enabled is true"
            )
        return self


class ToolsConfig(BaseModel):
    """Settings for local tool discovery and LLM-visible tool emission.

    ``enabled`` controls whether tool schemas are emitted to the upstream
    model.  When ``False``, the agent still discovers and registers tools
    (so subclasses can call them programmatically) but the streaming agent
    loop sends ``tools=None`` to the model.  Useful for vision-only or
    voice-only checkpoints served by vLLM that 400 when tool schemas are
    present.
    """

    local_dir: str = "./tools"
    visibility_default: Literal["agent_only", "llm_only", "both"] = "agent_only"
    enabled: bool = True


class PromptsConfig(BaseModel):
    """Settings for prompt template discovery."""

    dir: str = "./prompts"
    system: str = "system"


class IdentityConfig(BaseModel):
    """Identity layer (precedence 0): who the agent IS.

    Loaded from ``identity.md`` at the project root, or provided inline
    via the ``inline`` field.  Inline takes precedence over file.
    """

    source: str = "identity.md"
    inline: str | None = None
    enabled: bool = True


class PersonalityConfig(BaseModel):
    """Personality layer (precedence 1): HOW the agent behaves.

    Optional — off by default.  When enabled, loaded from
    ``personality.md`` at the project root.
    """

    source: str = "personality.md"
    enabled: bool = False


class PromptAssemblyConfig(BaseModel):
    """Named-layer prompt assembly configuration.

    When present on ``AgentConfig``, ``build_system_prompt()`` assembles
    the system message from four explicit layers (identity, personality,
    governance, capabilities) instead of the legacy flat concatenation.
    """

    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    governance_enabled: bool = True
    capabilities_enabled: bool = True


class TrustThresholdsConfig(BaseModel):
    """Score thresholds for trust level promotions (levels 1-4)."""

    level_1: float = 10.0
    level_2: float = 50.0
    level_3: float = 200.0
    level_4: float = 500.0


class SelfHealingConfig(BaseModel):
    """Self-healing configuration: learned skills and trust-scoped writes."""

    enabled: bool = False
    trust_level: int = Field(default=0, ge=0, le=4)
    trust_domains: list[str] = Field(default_factory=list)
    review_policy: Literal["audit_only", "peer_review", "human_review"] = (
        "human_review"
    )
    learned_skills_dir: str = "./learned_skills"
    max_skills: int = Field(default=50, ge=1)
    parent_agent_id: str | None = None
    parent_trust_level: int | None = None
    parent_capability_overlap: list[str] = Field(default_factory=list)
    seed_trust_level: int | None = None
    trust_thresholds: TrustThresholdsConfig = Field(
        default_factory=TrustThresholdsConfig
    )


class MaturationConfig(BaseModel):
    """Agent maturation lifecycle configuration."""

    enabled: bool = False
    apprentice_max_trust: int = Field(default=1, ge=0, le=4)
    journeyman_max_trust: int = Field(default=3, ge=0, le=4)
    specialist_min_trust: int = Field(default=4, ge=1, le=4)
    promotion_requires: Literal["auto", "human_approval"] = "auto"


class BackoffConfig(BaseModel):
    """Exponential backoff parameters for the agent loop."""

    initial: float = Field(default=1.0, gt=0.0)
    max: float = Field(default=30.0, gt=0.0)
    multiplier: float = Field(default=2.0, gt=1.0)

    @model_validator(mode="after")
    def _max_ge_initial(self) -> "BackoffConfig":
        if self.max < self.initial:
            raise ValueError(
                f"backoff.max ({self.max}) must be >= backoff.initial ({self.initial})"
            )
        return self


class LoopGuardConfig(BaseModel):
    """Doom-loop detector: fires when the same tool call repeats."""

    enabled: bool = True
    repeat_threshold: int = Field(default=3, ge=2)
    pattern_window: int = Field(default=5, ge=2)
    canonicalization: Literal["structured", "string"] = "structured"


class LoopConfig(BaseModel):
    """Agent loop execution parameters."""

    max_iterations: int = Field(default=100, gt=0)
    backoff: BackoffConfig = Field(default_factory=BackoffConfig)
    guard: LoopGuardConfig = Field(default_factory=LoopGuardConfig)

    @field_validator("max_iterations", mode="before")
    @classmethod
    def _coerce_max_iterations(cls, v: Any) -> Any:
        """Allow ``max_iterations`` to arrive as a string (from env var substitution)."""
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                raise ValueError(
                    f"loop.max_iterations must be an integer, got '{v}'"
                ) from None
        return v


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"

    @field_validator("level")
    @classmethod
    def _validate_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(
                f"logging.level must be one of {sorted(allowed)}, got '{v}'"
            )
        return upper


class MemoryConfig(BaseModel):
    """Memory backend settings.

    Controls which memory backend the agent uses.  When ``backend`` is
    unset (the default), the factory auto-detects by looking for
    ``.memoryhub.yaml`` — preserving backward compatibility.

    Supported backends:
      - ``memoryhub`` — MemoryHub SDK (requires ``memoryhub`` package)
      - ``markdown``  — Human-readable markdown file(s) (zero dependencies)
      - ``sqlite``    — Local SQLite with FTS5 (zero dependencies)
      - ``pgvector``  — PostgreSQL + pgvector (requires ``asyncpg``)
      - ``custom``    — Bring your own: set ``backend_class`` to a dotted
                        import path for a ``MemoryClientBase`` subclass
      - ``null``      — Explicitly disable memory

    Prefix injection:
      - ``prefix_role``      — Role for the memory prefix message: ``system``
                               (default, universal) or ``developer``
                               (harmony-format models like gpt-oss).
      - ``max_prefix_chars`` — Maximum character length for the memory prefix.
                               Prevents large backends from dumping their
                               entire store.  0 disables the limit.
      - ``injection_mode``   — Where to place retrieved memories:
                               ``prefix`` (default) inserts a separate message
                               before the user turn.  ``user_turn`` appends
                               memories to the user message inside XML tags,
                               which small models (8K-16K) treat as
                               higher-salience context.
      - ``injection_tag``    — XML tag name wrapping user-turn memories
                               (default ``user_memories``).  Only used when
                               ``injection_mode`` is ``user_turn``.

    Budget presets:
      - ``budget``           — Shorthand that sets defaults for
                               ``max_prefix_chars``, ``max_results``, and
                               ``min_weight`` based on model tier:

                               =======  ================  ===========  ==========
                               Budget   max_prefix_chars  max_results  min_weight
                               =======  ================  ===========  ==========
                               small    500               5            0.7
                               medium   4000              20           0.5
                               large    8000              50           0.3
                               =======  ================  ===========  ==========

                               Explicit field values always override the preset.
                               ``custom`` and ``None`` use field defaults.
      - ``max_results``      — Maximum number of memories to retrieve.
      - ``min_weight``       — Minimum weight threshold for retrieved memories.
                               Results below this weight are filtered out.

    Loading:
      - ``loading_pattern``  — When to retrieve memories.  ``eager``
                               (default when unset) loads at setup time.
                               ``lazy``, ``lazy_with_rebias``, and ``jit``
                               defer to after the first user message.
                               When set, overrides the pattern from
                               ``.memoryhub.yaml``.  Required for
                               file-based backends that want deferred loading.
    """

    _BUDGET_PRESETS: ClassVar[dict[str, dict[str, Any]]] = {
        "small": {"max_prefix_chars": 500, "max_results": 5, "min_weight": 0.7},
        "medium": {"max_prefix_chars": 4000, "max_results": 20, "min_weight": 0.5},
        "large": {"max_prefix_chars": 8000, "max_results": 50, "min_weight": 0.3},
    }

    backend: Literal["memoryhub", "markdown", "sqlite", "pgvector", "llamastack", "custom", "null"] | None = None
    config_path: str = ".memoryhub.yaml"
    backend_class: str | None = None
    prefix_role: Literal["system", "developer"] = "system"
    max_prefix_chars: int = 8000
    injection_mode: Literal["prefix", "user_turn"] = "prefix"
    injection_tag: str = "user_memories"
    budget: Literal["small", "medium", "large", "custom"] | None = None
    max_results: int = 50
    min_weight: float = 0.0
    loading_pattern: Literal["eager", "lazy", "lazy_with_rebias", "jit"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _apply_budget_presets(cls, data: Any) -> Any:
        """Fill in budget-controlled fields that the user didn't set."""
        if not isinstance(data, dict):
            return data
        budget = data.get("budget")
        if budget and budget in cls._BUDGET_PRESETS:
            for key, val in cls._BUDGET_PRESETS[budget].items():
                data.setdefault(key, val)
        return data

    @field_validator("backend", mode="before")
    @classmethod
    def _coerce_empty_backend(cls, v: Any) -> Any:
        """Coerce empty strings to None (from ``${MEMORY_BACKEND:-}``)."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v



class HookEntryConfig(BaseModel):
    """A single lifecycle hook declared in ``agent.yaml``."""

    event: str
    command: str
    timeout: float = Field(default=10.0, gt=0.0)
    matcher: str | None = None
    name: str | None = None


class ToolInspectionConfig(BaseModel):
    """Tool call inspection settings."""

    enabled: bool = True
    mode: Literal["enforce", "observe"] | None = None  # None = inherit from security.mode


class GuardrailsConfig(BaseModel):
    """Code execution guardrails settings."""

    mode: Literal["enforce", "observe"] | None = None


class SecurityConfig(BaseModel):
    """Security settings controlling inspection and audit behavior."""

    mode: Literal["enforce", "observe"] = "enforce"
    tool_inspection: ToolInspectionConfig = Field(
        default_factory=ToolInspectionConfig
    )
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)


class NodeConfig(BaseModel):
    """Configuration for a single workflow node's deployment topology."""

    type: Literal["local", "remote"] = "local"
    endpoint: str | None = None
    path: str = "/process"
    timeout: float = 30.0
    retries: int = 2

    @model_validator(mode="after")
    def _validate_remote_has_endpoint(self) -> "NodeConfig":
        if self.type == "remote" and not self.endpoint:
            raise ValueError("Remote nodes require an 'endpoint'")
        return self


class RemoteTransportConfig(BaseModel):
    """HTTP transport for a remote subagent.

    ``url`` is the OpenAI-compatible endpoint base (e.g.
    ``http://research-helper:8080/v1``).  Standard ``${VAR:-default}``
    env-var substitution applies before this model is validated.
    ``timeout_seconds`` caps the per-call wall time; subagent failures do
    not block the parent indefinitely.
    """

    type: Literal["remote"]
    url: str
    timeout_seconds: float = 60.0

    @field_validator("url", mode="before")
    @classmethod
    def _url_not_empty(cls, v: Any) -> Any:
        """Reject empty or None url values (env substitution may produce either)."""
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError(
                "transport.url is required for remote transport "
                "(env substitution may have produced an empty string)"
            )
        return v


class InProcessTransportConfig(BaseModel):
    """In-process transport for a subagent running in the same Python process.

    ``class_path`` is a dotted import path for a ``BaseAgent`` subclass
    (e.g. ``myagents.helper.HelperAgent``).  ``config_path`` is an
    optional path to the subagent's own ``agent.yaml``; when omitted the
    parent's config environment is inherited.

    .. note::
        ``identity: service_account: <name>`` is incompatible with this
        transport — there is no HTTP boundary at which to override the
        identity.  The validator on :class:`SubagentConfig` enforces this.
    """

    type: Literal["inprocess"]
    class_path: str
    config_path: str | None = None


TransportConfig = Annotated[
    Union[RemoteTransportConfig, InProcessTransportConfig],
    Field(discriminator="type"),
]
"""Discriminated union of supported subagent transports.

Select via ``transport.type: remote`` or ``transport.type: inprocess``
in ``agent.yaml``.
"""


_SUBAGENT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


class IdentityServiceAccount(BaseModel):
    """Explicit service-account identity override for a subagent.

    The subagent runs under the named kagenti-issued service-account
    credentials rather than inheriting the caller's identity.  Useful when
    the subagent has its own scoped credentials (e.g. an account-lookup
    agent with read-only DB access).
    """

    service_account: str


class SpawnConfig(BaseModel):
    """Ad-hoc spawn_agent tool configuration."""
    enabled: bool = True
    max_depth: int = Field(default=3, ge=1, le=10)
    max_iterations: int = Field(default=10, ge=1, le=100)
    allowed_models: list[str] | None = None


class SubagentConfig(BaseModel):
    """Configuration for a single registered subagent.

    Subagents are discovered by name via the ``subagents:`` block in
    ``agent.yaml`` and made available to the LLM as a ``delegate_to_agent``
    tool call.

    Fields
    ------
    name:
        Identifier the LLM uses in the ``agent_name`` tool parameter.
        Must match ``^[a-zA-Z][a-zA-Z0-9_]*$`` so it can be used safely
        as a tool argument value.
    description:
        Surfaced in the tool schema so the LLM knows what this subagent
        does.
    when_to_use:
        Selection hint baked into the tool's schema so the LLM has
        guidance on when to delegate.
    transport:
        Where and how to reach the subagent.  Either
        :class:`RemoteTransportConfig` (HTTP) or
        :class:`InProcessTransportConfig` (same process).
    permission_scope:
        References a named rule set in ``PermissionConfig.scopes`` (#164).
        The subagent runs under ``min(parent_scope, this_scope)``.
    identity:
        ``"inherit"`` (default) — the subagent carries the caller's
        identity.  ``service_account: <name>`` — the subagent runs under a
        fixed kagenti-issued identity.  Incompatible with
        ``transport.type: inprocess``.
    max_depth:
        Cap on delegation chains.  The framework tracks depth in the trace
        context and rejects calls that would exceed this limit.
    """

    name: str
    description: str
    when_to_use: str
    transport: TransportConfig
    permission_scope: str | None = None
    identity: Union[Literal["inherit"], IdentityServiceAccount] = "inherit"
    max_depth: int = Field(default=3, ge=1, le=10)

    @field_validator("name", mode="after")
    @classmethod
    def _name_valid_identifier(cls, v: str) -> str:
        if not _SUBAGENT_NAME_RE.match(v):
            raise ValueError(
                f"subagent name {v!r} is invalid: must match "
                r"^[a-zA-Z][a-zA-Z0-9_]*$ "
                "(start with a letter, contain only letters, digits, underscores)"
            )
        return v

    @model_validator(mode="after")
    def _inprocess_forbids_service_account(self) -> "SubagentConfig":
        if (
            self.transport.type == "inprocess"
            and isinstance(self.identity, IdentityServiceAccount)
        ):
            raise ValueError(
                f"Subagent {self.name!r}: identity 'service_account' is not supported "
                "for inprocess transport (no HTTP boundary to override identity at). "
                "Use identity: inherit, or switch to transport.type: remote."
            )
        return self


class AgentIdentity(BaseModel):
    """Agent name, description, and version for logging and API endpoints."""

    name: str = "agent"
    description: str = ""
    version: str = "0.1.0"


class StorageConfig(BaseModel):
    """Shared storage backend for sessions and traces.

    When ``backend`` is ``null`` (default), no persistence — features
    degrade gracefully to no-ops. ``sqlite`` uses a single file for
    both sessions and traces. ``postgres`` uses a shared connection pool.
    ``http`` delegates to a sibling ``fipsagents-platform`` service over
    REST; ``platform_url`` is required and ``platform_token`` is an
    optional static bearer token for service-to-service flows
    (per-request tokens forwarded from the inbound ``Authorization``
    header take precedence when present).
    """

    backend: Literal["sqlite", "postgres", "http"] | None = None
    sqlite_path: str = "./agent.db"
    database_url: str = ""
    platform_url: str = ""
    platform_token: str = ""

    @field_validator("backend", mode="before")
    @classmethod
    def _coerce_empty_backend(cls, v: Any) -> Any:
        """Coerce empty strings to None (from ``${STORAGE_BACKEND:-}``)."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("database_url", mode="before")
    @classmethod
    def _coerce_empty_url(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip() == "":
            return ""
        return v


class _PerStoreBackendMixin(BaseModel):
    """Per-store override for ``StorageConfig.backend``.

    When ``None`` the store inherits ``storage.backend``.  Allows mixing
    backends — eg ``feedback.backend: http`` while sessions/traces stay
    on local SQLite.
    """

    backend: Literal["sqlite", "postgres", "http"] | None = None

    @field_validator("backend", mode="before")
    @classmethod
    def _coerce_empty_backend(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


class SessionsConfig(_PerStoreBackendMixin):
    """Session persistence settings."""

    enabled: bool = False
    max_age_hours: int = Field(default=168, ge=0)


class TracesConfig(_PerStoreBackendMixin):
    """Trace collection settings."""

    enabled: bool = False
    max_age_hours: int = Field(default=168, ge=0)
    sampling_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    fidelity: Literal["minimal", "standard", "full"] = "minimal"
    exporter: Literal["store", "otel"] | None = None
    otel_endpoint: str | None = None
    service_name: str = "fipsagents"


class MetricsConfig(BaseModel):
    """Prometheus metrics settings.

    ``token_label_mode`` controls the label cardinality of
    ``agent_tokens_total``.  Each step up adds one dimension at the cost
    of more time-series stored by Prometheus.

    - ``model`` (default) — current behaviour, only ``model`` and
      ``direction`` labels.  Bounded by the model catalog.
    - ``tenant`` — also adds ``tenant_id`` (typically gateway-stamped via
      the ``X-Tenant`` header). Bounded by the tenant count, suitable for
      most enterprise deployments.
    - ``session`` — also adds ``session_id``.  **High cardinality**: one
      time-series per session per direction per model.  Only enable when
      you have an external aggregation step (eg Prometheus federation,
      Mimir) that can absorb the volume; otherwise prefer
      ``GET /v1/sessions/{id}/usage`` for per-session totals.
    """

    enabled: bool = False
    token_label_mode: Literal["model", "tenant", "session"] = "model"


class CompactionConfig(BaseModel):
    """Message compaction settings."""
    enabled: bool = False
    backend: Literal["null", "llm"] | None = None
    threshold_messages: int = Field(default=50, ge=1)
    keep_recent_turns: int = Field(default=4, ge=1)
    summary_role: Literal["system", "developer"] = "developer"
    summary_model: str | None = None
    context_limit: int = Field(default=0, ge=0)
    reserve_tokens: int = Field(default=4000, ge=0)

    @field_validator("backend", mode="before")
    @classmethod
    def _coerce_empty_backend(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


class GraphConfig(BaseModel):
    """Apache AGE property-graph store settings."""

    enabled: bool = False
    backend: Literal["null", "age"] = "null"
    database_url: str = ""
    graph_name: str = "agent_knowledge"

    @field_validator("backend", mode="before")
    @classmethod
    def _coerce_empty_backend(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip() == "":
            return "null"
        return v


class CapabilityConfig(BaseModel):
    """A capability this agent possesses for work-item matching."""
    name: str
    value: float = Field(default=1.0, ge=0.0)


class WorkItemsConfig(_PerStoreBackendMixin):
    """Work-item pool coordination settings."""
    enabled: bool = False
    lease_duration_seconds: int = Field(default=300, ge=30)
    budget_headroom_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    expire_check_interval_seconds: int = Field(default=60, ge=10)
    capabilities: list[CapabilityConfig] = Field(default_factory=list)


class PermissionRuleConfig(BaseModel):
    """A single declarative permission rule."""
    id: str | None = None
    tool: str = "*"
    action: Literal["allow", "deny", "ask"] = "allow"
    scope: str | None = None
    reason: str | None = None


class PermissionConfig(BaseModel):
    """Permission resolution settings."""
    source: Literal["null", "static"] | None = None
    default_action: Literal["allow", "deny", "ask"] = "allow"
    mode: Literal["enforce", "observe"] = "enforce"
    rules: list[PermissionRuleConfig] = Field(default_factory=list)

    @field_validator("source", mode="before")
    @classmethod
    def _coerce_empty_source(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


class FeedbackConfig(_PerStoreBackendMixin):
    """Feedback collection settings."""

    enabled: bool = False
    max_age_hours: int = Field(default=720, ge=0)


class ScannerConfig(BaseModel):
    """Virus-scanning sidecar settings.

    The scanner runs between MIME sniffing and parsing on every
    upload. It speaks HTTP to a sidecar that wraps ClamAV (or any
    other engine that matches the contract): POST the file bytes,
    expect a JSON body of ``{"infected": bool, "viruses": [str]}`` or
    a 200/422 status.

    ``fail_mode`` controls behavior when the scanner sidecar is
    unreachable or errors:

    - ``open`` (default) — accept the upload and log a warning. Right
      for non-production / dev environments where occasional sidecar
      hiccups should not break the API.
    - ``closed`` — reject the upload with HTTP 503. Right for
      production where every file must be scanned before storage.

    When ``url`` is empty (default), no scanner is configured and the
    upload path runs without virus checks.
    """

    url: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    fail_mode: Literal["open", "closed"] = "open"


class BytesBackendConfig(BaseModel):
    """Bytes-storage backend (per ADR-0001).

    Composes with the metadata ``backend`` to give multi-target file
    storage:

    - ``type: local_fs`` (default) — sharded local filesystem at
      :attr:`FilesConfig.bytes_dir`. Single-replica only.
    - ``type: s3`` — S3-compatible object storage (AWS S3, MinIO,
      GCS S3-mode, Cloudflare R2, Backblaze B2). Requires the
      ``[s3]`` extra (``pip install fipsagents[s3]``).
    - ``type: null`` — bytes are accepted then discarded; useful in
      tests and dry-run modes.

    For S3 deployments, ``access_key`` / ``secret_key`` are optional
    when boto3's default credential chain is sufficient (IAM role, env
    vars, EC2 metadata service, etc.).
    """

    type: Literal["local_fs", "s3", "null"] = "local_fs"
    bucket: str = ""
    endpoint: str = ""
    region: str = "us-east-1"
    access_key: str = ""
    secret_key: str = ""
    prefix: str = ""
    path_style: bool = False


class PdfParserConfig(BaseModel):
    """Docling PDF pipeline knobs.

    Maps directly onto :class:`docling.datamodel.pipeline_options.PdfPipelineOptions`.
    Only the high-impact fields are surfaced; other Docling defaults are
    used as-is.

    ``do_ocr=False`` is the framework default (changed in 0.19.0). Most
    modern PDFs ship a selectable text layer and OCR adds 1-2 seconds
    per page on a 2-CPU pod with no quality benefit. Operators with
    scanned PDFs flip it back on.
    """

    do_ocr: bool = False
    do_table_structure: bool = True


class ParserConfig(BaseModel):
    """File-parser settings.

    Currently only PDF has a Docling-specific pipeline; other formats
    use the converter defaults. Sub-blocks (``docx``, ``pptx``, ...)
    can be added without breaking existing configs.
    """

    pdf: PdfParserConfig = Field(default_factory=PdfParserConfig)


class ChunkingConfig(BaseModel):
    """Large-file chunking + retrieval settings (per ADR-0002).

    When enabled, files whose extracted text exceeds
    ``small_file_threshold_tokens`` are split into chunks, embedded, and
    stored in a vector database. At chat-completion time the chunked
    file's content is retrieved per-query instead of dumping the full
    text into the prompt — the canonical RAG path scoped to the user's
    referenced ``file_ids``.

    ``backend: null`` (the default) keeps the 0.17.0 full-text behavior.
    ``backend: pgvector`` requires ``database_url`` and ``embedding_url``
    plus the ``[chunking]`` extra installed.

    ``budget`` mirrors :class:`MemoryConfig.budget` — selecting a preset
    sets sensible defaults for ``chunk_size_tokens``,
    ``small_file_threshold_tokens``, and ``retrieval_top_k``. Explicit
    values always override the preset.

    ``chunking.enabled: false`` is the universal default; existing
    deployments upgrade with no behavior change.
    """

    _BUDGET_PRESETS: ClassVar[dict[str, dict[str, Any]]] = {
        "small": {
            "chunk_size_tokens": 400,
            "retrieval_top_k": 3,
            "small_file_threshold_tokens": 2000,
        },
        "medium": {
            "chunk_size_tokens": 600,
            "retrieval_top_k": 5,
            "small_file_threshold_tokens": 4000,
        },
        "large": {
            "chunk_size_tokens": 800,
            "retrieval_top_k": 8,
            "small_file_threshold_tokens": 8000,
        },
    }

    enabled: bool = False
    backend: Literal["null", "pgvector"] = "null"
    database_url: str = ""
    embedding_url: str = ""
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimension: int = Field(default=768, gt=0)
    table_name: str = "file_chunks"
    budget: Literal["small", "medium", "large", "custom"] | None = None
    chunk_size_tokens: int = Field(default=600, gt=0)
    chunk_overlap_tokens: int = Field(default=100, ge=0)
    small_file_threshold_tokens: int = Field(default=4000, ge=0)
    retrieval_top_k: int = Field(default=5, ge=1)
    retrieval_min_score: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def _apply_budget_presets(cls, data: Any) -> Any:
        """Fill in budget-controlled fields the user didn't set."""
        if not isinstance(data, dict):
            return data
        budget = data.get("budget")
        if budget and budget in cls._BUDGET_PRESETS:
            for key, val in cls._BUDGET_PRESETS[budget].items():
                data.setdefault(key, val)
        return data


class FilesConfig(_PerStoreBackendMixin):
    """File upload settings.

    ``bytes_dir`` is the local-FS root used when
    ``bytes_backend.type == "local_fs"`` (default for backward
    compatibility with 0.16.0). For S3-compatible storage, set
    ``bytes_backend.type: s3`` plus ``bucket`` / ``endpoint`` /
    credentials.

    ``allowed_mime_types`` is enforced by the ``POST /v1/files``
    endpoint when present (an empty list disables the allowlist).

    ``sqlite_path`` overrides ``storage.sqlite_path`` for the file store
    only — useful when ``bytes_dir`` is on a PVC and the metadata DB
    should be co-located on the same volume (so both bytes and metadata
    survive pod restarts). Empty defers to ``storage.sqlite_path``.

    ``chunking`` is the optional retrieval-augmentation layer (per
    ADR-0002). Disabled by default; enable to chunk large files at
    upload time and retrieve only the relevant chunks at chat-completion
    time instead of injecting the full extracted text.

    ``parser`` exposes Docling pipeline knobs. The 0.19.0 default flips
    ``parser.pdf.do_ocr`` to ``False`` -- text-extractable PDFs parse in
    sub-second instead of multiple minutes. Operators with scanned
    PDFs set ``do_ocr: true``.

    ``max_injection_tokens`` caps how many tokens of extracted text
    the full-text injection path inserts per file.  When the extracted
    text exceeds this limit it is truncated and a note is appended
    directing operators to enable chunking for full-content retrieval.
    This prevents oversized documents from blowing out the model's
    context window (which causes vLLM to compute a negative
    ``max_tokens`` and reject the request).  Set to ``0`` to disable
    the guard entirely.
    """

    enabled: bool = False
    max_file_size_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    max_injection_tokens: int = Field(default=100_000, ge=0)
    bytes_dir: str = "./files"
    bytes_backend: BytesBackendConfig = Field(default_factory=BytesBackendConfig)
    sqlite_path: str = ""
    allowed_mime_types: list[str] = Field(default_factory=list)
    max_age_hours: int = Field(default=720, ge=0)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    parser: ParserConfig = Field(default_factory=ParserConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)


class PricingRate(BaseModel):
    """Per-token / per-request pricing for a single model.

    All rates are USD. Token rates are quoted per 1,000 tokens to match
    public model-pricing tables (OpenAI, Anthropic, Bedrock). For
    self-hosted vLLM deployments without dollar billing, leave the
    defaults at zero -- :func:`fipsagents.server.pricing.compute_cost`
    will return ``0.0`` and the new ``/usage`` endpoint will surface a
    no-op cost line.

    ``cached_input_per_1k`` covers prompt-cache hits when the provider
    returns a ``prompt_tokens_details.cached_tokens`` count; OpenAI's
    semantics treat cached tokens as a subset of ``prompt_tokens`` billed
    at a reduced rate. ``None`` means "no cached-tier discount" and the
    full ``input_per_1k`` rate applies.
    """

    input_per_1k: float = Field(default=0.0, ge=0.0)
    output_per_1k: float = Field(default=0.0, ge=0.0)
    cached_input_per_1k: float | None = Field(default=None, ge=0.0)
    per_request: float = Field(default=0.0, ge=0.0)


class PricingConfig(BaseModel):
    """Token cost lookup table.

    ``default`` applies to any model not listed in ``models``. ``models``
    keys must match the model identifier exactly as it appears on
    completion requests (typically ``model.name`` from ``agent.yaml``).
    """

    default: PricingRate = Field(default_factory=PricingRate)
    models: dict[str, PricingRate] = Field(default_factory=dict)


class BudgetLimits(BaseModel):
    """Soft (warn) and hard (enforce) USD limits.

    ``warn_usd`` is logged when the running total crosses it. ``limit_usd``
    triggers :class:`fipsagents.server.budget.BudgetExceededError` (HTTP 402)
    when ``budget.mode`` is ``enforce``. Setting either to ``0`` (the default)
    disables that threshold.
    """

    warn_usd: float = Field(default=0.0, ge=0.0)
    limit_usd: float = Field(default=0.0, ge=0.0)


class BudgetConfig(BaseModel):
    """Per-session and per-tenant cost budgets.

    Per-session budgets read cumulative ``cost_data`` from the session
    store and convert to USD via :class:`PricingConfig`.  Per-tenant
    budgets aggregate session deltas in-process — accurate for
    single-replica deployments and for "this agent's view" of cross-session
    tenant cost.  Multi-replica tenant aggregation requires a separate
    cross-agent service and is out of scope here.

    ``mode``:

    - ``observe`` — log warnings + limit crossings, never raise.
    - ``enforce`` (default) — raise ``BudgetExceededError`` on hard limit.
    """

    mode: Literal["observe", "enforce"] = "enforce"
    per_session: BudgetLimits = Field(default_factory=BudgetLimits)
    per_tenant: BudgetLimits = Field(default_factory=BudgetLimits)

    def is_active(self) -> bool:
        """True if any limit is configured (warn or hard, session or tenant)."""
        return any(
            v > 0.0 for v in (
                self.per_session.warn_usd,
                self.per_session.limit_usd,
                self.per_tenant.warn_usd,
                self.per_tenant.limit_usd,
            )
        )


class EventRetryConfig(BaseModel):
    """Retry parameters for event processing."""

    max_attempts: int = Field(default=3, ge=1)
    backoff_base: float = Field(default=2.0, gt=0)
    backoff_max: float = Field(default=60.0, gt=0)
    retriable_errors: list[str] = Field(
        default_factory=lambda: ["TimeoutError"],
    )


class WebhookSourceConfig(BaseModel):
    """Configuration for a webhook event source."""

    type: Literal["webhook"]
    source_id: str | None = None
    path: str
    secret: str | None = None
    event_type_header: str = "X-GitHub-Event"
    signature_header: str = "X-Hub-Signature-256"
    session_ttl_hours: int = Field(default=168, ge=0)
    max_events_per_second: float = Field(default=10.0, ge=0)
    retry: EventRetryConfig = Field(default_factory=EventRetryConfig)

    @field_validator("source_id", "secret", mode="before")
    @classmethod
    def _coerce_empty(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class CronSourceConfig(BaseModel):
    """Configuration for a cron event source."""

    type: Literal["cron"]
    source_id: str | None = None
    schedule: str
    event_type: str
    session_ttl_hours: int = Field(default=168, ge=0)
    max_events_per_second: float = Field(default=1.0, ge=0)
    retry: EventRetryConfig = Field(default_factory=EventRetryConfig)

    @field_validator("source_id", mode="before")
    @classmethod
    def _coerce_empty(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class KafkaSourceConfig(BaseModel):
    """Configuration for a Kafka event source."""

    type: Literal["kafka"]
    source_id: str | None = None
    bootstrap_servers: str
    topic: str
    consumer_group: str
    auto_offset_reset: str = "latest"
    security_protocol: str | None = None
    sasl_mechanism: str | None = None
    sasl_username: str | None = None
    sasl_password: str | None = None
    session_ttl_hours: int = Field(default=168, ge=0)
    max_events_per_second: float = Field(default=10.0, ge=0)
    retry: EventRetryConfig = Field(default_factory=EventRetryConfig)

    @field_validator("source_id", mode="before")
    @classmethod
    def _coerce_empty(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class RedisSourceConfig(BaseModel):
    """Configuration for a Redis Streams event source."""

    type: Literal["redis"]
    source_id: str | None = None
    url: str
    stream: str
    consumer_group: str
    consumer_name: str = "worker-0"
    block_ms: int = Field(default=5000, ge=0)
    session_ttl_hours: int = Field(default=168, ge=0)
    max_events_per_second: float = Field(default=10.0, ge=0)
    retry: EventRetryConfig = Field(default_factory=EventRetryConfig)

    @field_validator("source_id", mode="before")
    @classmethod
    def _coerce_empty(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class NullSourceConfig(BaseModel):
    """Configuration for a null (no-op) event source."""

    type: Literal["null"]
    source_id: str = "null"


class NullSinkConfig(BaseModel):
    """Configuration for a null (no-op) event sink."""

    type: Literal["null"]


class LogSinkConfig(BaseModel):
    """Configuration for a log event sink."""

    type: Literal["log"]
    level: str = "INFO"


class HttpCallbackSinkConfig(BaseModel):
    """Configuration for an HTTP callback event sink."""

    type: Literal["http_callback"]
    url: str
    timeout_seconds: float = Field(default=30.0, gt=0)


class KafkaSinkConfig(BaseModel):
    """Configuration for a Kafka event sink."""

    type: Literal["kafka"]
    bootstrap_servers: str
    topic: str
    security_protocol: str | None = None
    sasl_mechanism: str | None = None
    sasl_username: str | None = None
    sasl_password: str | None = None


class RedisSinkConfig(BaseModel):
    """Configuration for a Redis Streams event sink."""

    type: Literal["redis"]
    url: str
    stream: str
    maxlen: int | None = None


EventSourceConfig = Annotated[
    Union[
        WebhookSourceConfig,
        CronSourceConfig,
        KafkaSourceConfig,
        RedisSourceConfig,
        NullSourceConfig,
    ],
    Field(discriminator="type"),
]

EventSinkConfig = Annotated[
    Union[
        NullSinkConfig,
        LogSinkConfig,
        HttpCallbackSinkConfig,
        KafkaSinkConfig,
        RedisSinkConfig,
    ],
    Field(discriminator="type"),
]


class StateRecoveryConfig(BaseModel):
    """Reducer-based state recovery settings."""
    enabled: bool = False


class ServerConfig(BaseModel):
    """HTTP server binding and feature configuration."""

    host: str = "0.0.0.0"
    port: int = Field(default=8080, gt=0, le=65535)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    traces: TracesConfig = Field(default_factory=TracesConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    state_recovery: StateRecoveryConfig = Field(default_factory=StateRecoveryConfig)
    event_sources: list[EventSourceConfig] = Field(default_factory=list)
    event_sink: EventSinkConfig | None = None
    graph: GraphConfig = Field(default_factory=GraphConfig)
    work_items: WorkItemsConfig = Field(default_factory=WorkItemsConfig)

    @field_validator("port", mode="before")
    @classmethod
    def _coerce_port(cls, v: Any) -> Any:
        """Coerce string port values from env-var substitution."""
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                raise ValueError(
                    f"server.port must be an integer, got '{v}'"
                ) from None
        return v


class AgentConfig(BaseModel):
    """Top-level agent configuration, corresponding to ``agent.yaml``."""

    agent: AgentIdentity = Field(default_factory=AgentIdentity)
    model: LLMConfig = Field(default_factory=LLMConfig)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    nodes: dict[str, NodeConfig] = Field(default_factory=dict)
    subagents: list[SubagentConfig] = Field(default_factory=list)
    prompt_assembly: PromptAssemblyConfig | None = None
    self_healing: SelfHealingConfig = Field(default_factory=SelfHealingConfig)
    maturation: MaturationConfig = Field(default_factory=MaturationConfig)
    spawn: SpawnConfig = Field(default_factory=SpawnConfig)
    hooks: list[HookEntryConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_subagent_names(self) -> "AgentConfig":
        seen: set[str] = set()
        duplicates: list[str] = []
        for sa in self.subagents:
            if sa.name in seen:
                duplicates.append(sa.name)
            seen.add(sa.name)
        if duplicates:
            raise ValueError(
                f"Duplicate subagent name(s) in agent.yaml: "
                f"{', '.join(sorted(set(duplicates)))}. "
                "Each subagent must have a unique name."
            )
        return self


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def parse_yaml_with_env(
    raw: str,
    *,
    env: dict[str, str] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Parse a YAML string after resolving ``${VAR:-default}`` placeholders.

    Parameters
    ----------
    raw:
        Raw YAML content (may contain env var placeholders).
    env:
        Custom environment mapping.  Defaults to ``os.environ``.
    strict:
        Raise on unresolved variables that have no default.

    Returns
    -------
    dict:
        The parsed, substituted YAML as a plain dictionary.

    Raises
    ------
    ConfigError:
        On YAML syntax errors or (when *strict*) unresolved variables.
    """
    substituted = substitute_env_vars(raw, env=env, strict=strict)
    try:
        data = yaml.safe_load(substituted)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in agent config: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"agent.yaml must be a YAML mapping at the top level, "
            f"got {type(data).__name__}"
        )
    return data


def load_config(
    path: str | Path = "agent.yaml",
    *,
    env: dict[str, str] | None = None,
    strict: bool = False,
) -> AgentConfig:
    """Load and validate agent configuration from a YAML file.

    Parameters
    ----------
    path:
        Path to the YAML configuration file.
    env:
        Custom environment mapping.  Defaults to ``os.environ``.
    strict:
        Raise on unresolved environment variables that lack defaults.

    Returns
    -------
    AgentConfig:
        Fully validated configuration.

    Raises
    ------
    ConfigError:
        When the file cannot be read, the YAML is invalid, or
        validation fails.
    """
    filepath = Path(path)
    if not filepath.exists():
        raise ConfigError(
            f"Configuration file not found: {filepath.resolve()}\n"
            f"Create an agent.yaml or pass an explicit path to load_config()."
        )
    try:
        raw = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read {filepath}: {exc}") from exc

    data = parse_yaml_with_env(raw, env=env, strict=strict)

    try:
        config = AgentConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"Invalid agent configuration: {exc}") from exc

    _stash_header_templates(config, raw)
    return config


def load_config_from_string(
    raw: str,
    *,
    env: dict[str, str] | None = None,
    strict: bool = False,
) -> AgentConfig:
    """Load and validate agent configuration from a YAML string.

    Useful for testing or when the config is assembled programmatically.
    """
    data = parse_yaml_with_env(raw, env=env, strict=strict)
    try:
        config = AgentConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"Invalid agent configuration: {exc}") from exc

    _stash_header_templates(config, raw)
    return config


def _stash_header_templates(config: "AgentConfig", raw_yaml: str) -> None:
    """Preserve raw header templates on MCP server configs for reconnection.

    The raw YAML is parsed *without* env-var substitution so that header
    values containing ``${VAR}`` patterns survive for later re-resolution
    (e.g. after an ``mcp_auth_refresh`` hook updates the env var).
    """
    try:
        raw_data = yaml.safe_load(raw_yaml)
    except Exception:
        return
    if not isinstance(raw_data, dict):
        return
    servers = raw_data.get("mcp_servers")
    if not isinstance(servers, list):
        return
    for i, entry in enumerate(servers):
        if i >= len(config.mcp_servers):
            break
        if isinstance(entry, dict) and entry.get("headers"):
            config.mcp_servers[i]._header_templates = entry["headers"]
