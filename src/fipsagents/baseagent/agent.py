"""BaseAgent — the core integration layer for production-ready AI agents.

Wires together LLM communication, tool dispatch, prompt/skill/rule loading,
memory integration, and MCP server connections.  Subclasses implement
``step()`` with ~20-30 lines of agent logic; everything else is here.

Lifecycle: ``setup()`` -> ``run()`` (loops ``step()``) -> ``shutdown()``
"""

from __future__ import annotations

import abc
import asyncio
import enum
import logging
import os as _os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, TypeVar

from fipsagents.baseagent.config import (
    AgentConfig,
    McpServerConfig,
    load_config,
    _ADAPTER_ENDPOINT,
    _OFF_PLATFORM_PROVIDERS,
)
from fipsagents.baseagent.hooks import HookRunner, create_hook_runner
from fipsagents.baseagent.events import (
    BudgetHeadroomWarning,
    ContentDelta,
    LimitExceeded,
    LoopBreakEvent,
    PermissionDecisionMade,
    QuestionAsked,
    ReasoningDelta,
    StreamComplete,
    StreamEvent,
    StreamMetrics,
    ToolCallDelta,
    ToolResultEvent,
)
from fipsagents.baseagent.llm import (
    LLMClient,
    ModelResponse,
    ModerationResult,
    PlatformResponse,
)
from fipsagents.baseagent.reasoning import ThinkTagParser, create_reasoning_parser
from fipsagents.baseagent.memory import MemoryClientBase, NullMemoryClient, create_memory_client
from fipsagents.baseagent.prompts import PromptLoader, PromptNotFoundError
from fipsagents.baseagent.pricing import compute_cost
from fipsagents.baseagent.rules import RuleLoader
from fipsagents.baseagent.skills import SkillLoader
from fipsagents.baseagent.tools import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message ID utilities
# ---------------------------------------------------------------------------


def _generate_message_id() -> str:
    """Sortable timestamp+random ID for stable message references."""
    ms = int(time.time() * 1000)
    rand = _os.urandom(6).hex()
    return f"msg_{ms:012x}_{rand}"


def _stamp_message_id(msg: dict) -> dict:
    """Ensure *msg* has an ``id`` key. Mutates in place and returns *msg*."""
    if "id" not in msg:
        msg["id"] = _generate_message_id()
    return msg

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Mutable MCP client reference — allows token refresh without re-registering tools
# ---------------------------------------------------------------------------


class _McpClientRef:
    """Mutable container for an MCP client reference.

    Tool closures capture this ref instead of the client directly.
    On reconnect, ``ref.client`` is swapped and all closures immediately
    see the fresh client.
    """

    __slots__ = ("client", "label", "config", "header_templates", "_reconnect_lock")

    def __init__(
        self,
        client: Any,
        label: str,
        config: Any | None = None,
        header_templates: dict[str, str] | None = None,
    ) -> None:
        self.client = client
        self.label = label
        self.config = config
        self.header_templates = header_templates
        self._reconnect_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Step result — returned by each step() invocation
# ---------------------------------------------------------------------------


class StepOutcome(enum.Enum):
    """Whether the agent loop should continue or stop."""

    CONTINUE = "continue"
    DONE = "done"


@dataclass
class StepResult:
    """Outcome of a single agent step."""

    outcome: StepOutcome
    result: Any = None

    @classmethod
    def continue_(cls) -> StepResult:
        return cls(outcome=StepOutcome.CONTINUE)

    @classmethod
    def done(cls, result: Any = None) -> StepResult:
        return cls(outcome=StepOutcome.DONE, result=result)


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


class BaseAgent(abc.ABC):
    """Abstract base for all agents.

    Subclasses implement :meth:`step` — one iteration of agent logic.
    Everything else (LLM, tools, prompts, MCP, memory, lifecycle) is
    provided here.
    """

    state_type: type | None = None

    def __init__(
        self,
        config_path: str | Path = "agent.yaml",
        *,
        config: AgentConfig | None = None,
        base_dir: str | Path | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._provided_config = config
        self._base_dir = Path(base_dir) if base_dir else None

        # Subsystem instances — populated by setup().
        self.config: AgentConfig | None = None
        self.llm: LLMClient | None = None
        self.tools: ToolRegistry = ToolRegistry()
        self.prompts: PromptLoader = PromptLoader()
        self.skills: SkillLoader = SkillLoader()
        self.rules: RuleLoader = RuleLoader()
        self.memory: MemoryClientBase = NullMemoryClient()
        self.hooks: HookRunner = HookRunner()
        self._assembler: Any = None

        # Conversation state.
        self.messages: list[dict[str, Any]] = []

        # MCP client references for cleanup and auth refresh.
        self._mcp_clients: list[_McpClientRef] = []

        # MCP prompts, resources, and resource templates — populated by connect_mcp().
        self._mcp_prompts: dict[str, tuple[Any, Any]] = {}      # name → (client, mcp.types.Prompt)
        self._mcp_resources: dict[str, tuple[Any, Any]] = {}     # uri_string → (client, mcp.types.Resource)
        self._mcp_resource_templates: dict[str, tuple[Any, Any]] = {}  # uri_template → (client, mcp.types.ResourceTemplate)

        # Subagent registry — populated by setup() from config.subagents.
        self.subagents: dict[str, Any] = {}
        # Append-only buffer drained by astep_stream; appended to by the
        # delegate_to_agent tool.
        self._subagent_events: list[StreamEvent] = []
        # Append-only token-usage list drained by the server's per-turn
        # cost-roll-up (_persist_cost_data). Each entry is a dict with
        # "input"/"output"/"cached" keys.
        self._subagent_token_usage: list[dict[str, int]] = []
        # Current depth in the delegation chain. 0 for a top-level agent.
        self._delegation_depth: int = 0
        # Forwarded for ``identity: inherit`` subagents. Populated by the
        # server before calling astep_stream when the incoming request
        # carries an Authorization header.
        self._inbound_auth_header: str | None = None

        # Question tool: pending question state set by ask_user.
        self._question_pending: dict[str, Any] | None = None
        self._question_events: list[StreamEvent] = []

        # Permission source — set by the server before astep_stream().
        self._permission_source: Any | None = None
        self._permission_mode: str = "enforce"
        self._permission_preapproved: set[str] = set()

        # Reducer state — set per-session by the server layer.
        self._agent_state: Any | None = None

        # Work-item store — set by the server before astep_stream().
        self._work_item_store: Any = None
        self._work_item_actor_id: str | None = None
        self._work_item_events: list[Any] = []

        # Self-healing events buffer — drained by astep_stream.
        self._self_healing_events: list[Any] = []

        # Trust manager — initialized in setup() when self_healing is enabled.
        self._trust_manager: Any = None

        # Maturation manager — initialized in setup() when maturation is enabled.
        self._maturation_manager: Any = None

        # Capability auto-discovery — populated by _discover_capabilities()
        # at the end of setup(). Used for work-item matching.
        self._discovered_capabilities: list[Any] = []
        self._checked_out_work_item: Any = None
        self._headroom_warned: bool = False

        # Tracks whether setup has completed.
        self._setup_done = False

    # -- Lifecycle -----------------------------------------------------------

    async def setup(self) -> None:
        """Initialise all subsystems.  Call once before :meth:`run`."""
        # 1. Configuration
        if self._provided_config is not None:
            self.config = self._provided_config
        else:
            self.config = load_config(self._config_path)

        base = self._base_dir or self._config_path.parent

        # 2. Logging
        logging.basicConfig(level=self.config.logging.level)

        logger.info(
            "Setting up agent — provider=%s, model=%s, endpoint=%s",
            self.config.model.provider,
            self.config.model.name,
            self.config.model.endpoint,
        )

        # 3. LLM client
        #    For off-platform providers, the adapter sidecar translates
        #    requests from OpenAI wire format to the provider's native API.
        #    Rewrite the endpoint to the sidecar before constructing the client.
        effective_model_cfg = self.config.model
        if self.config.model.provider in _OFF_PLATFORM_PROVIDERS:
            if self.config.model.endpoint is not None:
                logger.warning(
                    "model.endpoint (%s) is ignored when provider=%s; "
                    "traffic is routed to the adapter sidecar at %s",
                    self.config.model.endpoint,
                    self.config.model.provider,
                    _ADAPTER_ENDPOINT,
                )
            effective_model_cfg = self.config.model.model_copy(
                update={"endpoint": _ADAPTER_ENDPOINT},
            )
            logger.info(
                "Provider=%s — routing LLM traffic through adapter "
                "sidecar at %s",
                self.config.model.provider,
                _ADAPTER_ENDPOINT,
            )
        self.llm = LLMClient(effective_model_cfg, platform=self.config.platform)

        # 4. Tool discovery
        tools_dir = base / self.config.tools.local_dir
        discovered = self.tools.discover(tools_dir)
        logger.info("Discovered %d local tool(s)", len(discovered))

        # 4a. Subagent registry (must precede stock tool discovery)
        self.subagents = {sa.name: sa for sa in self.config.subagents}

        # 4b. Stock framework tools (delegate_to_agent, ask_user, etc.)
        stock = self.tools.discover_stock(self)
        logger.info("Registered %d stock tool(s)", len(stock))

        # 4c. Tool inspection
        if self.config.security.tool_inspection.enabled:
            from fipsagents.baseagent.tool_inspector import ToolInspector

            inspector = ToolInspector()
            effective_mode = (
                self.config.security.tool_inspection.mode
                or self.config.security.mode
            )
            self.tools.set_inspector(inspector, mode=effective_mode)
            logger.info(
                "Tool inspection enabled (mode=%s)", effective_mode
            )

        # 5. Prompts
        prompts_dir = base / self.config.prompts.dir
        if prompts_dir.is_dir():
            loaded = self.prompts.load_all(prompts_dir)
            logger.info("Loaded %d prompt(s)", len(loaded))
        else:
            logger.debug("Prompts directory does not exist: %s", prompts_dir)

        # 6. Skills
        skills_dir = base / "skills"
        if skills_dir.is_dir():
            stubs = self.skills.load_all(skills_dir)
            logger.info("Discovered %d skill stub(s)", len(stubs))
        else:
            logger.debug("Skills directory does not exist: %s", skills_dir)

        # 6a. Learned skills (when self_healing is enabled).
        if self.config.self_healing.enabled:
            learned_dir = base / self.config.self_healing.learned_skills_dir
            if learned_dir.is_dir():
                loaded_learned = self.skills.load_learned(learned_dir)
                if loaded_learned:
                    logger.info("Loaded %d learned skill(s)", len(loaded_learned))

        # 6b. Trust manager (when self_healing is enabled).
        if self.config.self_healing.enabled:
            from fipsagents.baseagent.trust import TrustManager

            th = self.config.self_healing.trust_thresholds
            self._trust_manager = TrustManager(
                thresholds=(th.level_1, th.level_2, th.level_3, th.level_4),
            )

            # Seed trust from parent lineage if configured.
            sh = self.config.self_healing
            if sh.parent_trust_level is not None:
                self._trust_manager.seed_from_parent(
                    parent_trust_level=sh.parent_trust_level,
                    capability_overlap=sh.parent_capability_overlap,
                    seed_level=sh.seed_trust_level,
                )

            logger.info(
                "Trust manager initialized (level=%d)", self._trust_manager.level
            )

        # 6c. Maturation manager (when maturation is enabled).
        if self.config.maturation.enabled:
            from fipsagents.baseagent.maturation import MaturationManager

            trust_mgr = getattr(self, "_trust_manager", None)
            if trust_mgr is not None:
                self._maturation_manager = MaturationManager(
                    trust_mgr,
                    apprentice_max_trust=self.config.maturation.apprentice_max_trust,
                    journeyman_max_trust=self.config.maturation.journeyman_max_trust,
                    specialist_min_trust=self.config.maturation.specialist_min_trust,
                )
                logger.info(
                    "Maturation manager initialized (stage=%s)",
                    self._maturation_manager.current_stage().value,
                )
            else:
                logger.warning(
                    "Maturation enabled but self_healing is disabled — "
                    "maturation requires trust tracking"
                )

        # 7. Rules
        rules_dir = base / "rules"
        if rules_dir.is_dir():
            loaded_rules = self.rules.load_all(rules_dir)
            logger.info("Loaded %d rule(s)", len(loaded_rules))
        else:
            logger.debug("Rules directory does not exist: %s", rules_dir)

        # 7a. Prompt assembler (when prompt_assembly config is present).
        if self.config.prompt_assembly is not None:
            from fipsagents.baseagent.prompt_assembly import PromptAssembler
            pa = self.config.prompt_assembly
            self._assembler = PromptAssembler(
                identity_source=pa.identity.source,
                identity_inline=pa.identity.inline,
                identity_enabled=pa.identity.enabled,
                personality_source=pa.personality.source,
                personality_enabled=pa.personality.enabled,
                governance_enabled=pa.governance_enabled,
                capabilities_enabled=pa.capabilities_enabled,
                base_dir=base,
                prompts=self.prompts,
                rules=self.rules,
                skills=self.skills,
                system_prompt_name=(
                    self.config.prompts.system if self.config else "system"
                ),
            )
            logger.info("Prompt assembler initialized (layered mode)")

        # 7b. Lifecycle hooks
        self.hooks = create_hook_runner(
            config_hooks=self.config.hooks,
            hooks_dir=base / "hooks",
        )
        if self.hooks:
            logger.info("Loaded %d lifecycle hook(s)", len(self.hooks))

        # 8. Memory
        memory_cfg_path = base / self.config.memory.config_path
        self.memory = await create_memory_client(
            memory_cfg_path, config=self.config.memory
        )

        # 9. MCP servers
        if self.config.platform.enabled:
            # In platform mode OGX orchestrates MCP server-side; the agent
            # never opens its own FastMCP client connections. Surface the
            # skip so deployments that misconfigure both blocks see it.
            if self.config.mcp_servers:
                logger.info(
                    "platform.enabled=true — skipping client-side connect_mcp() "
                    "for %d configured mcp_servers; OGX will orchestrate "
                    "%d platform.mcp entries server-side",
                    len(self.config.mcp_servers),
                    len(self.config.platform.mcp),
                )
            else:
                logger.info(
                    "platform.enabled=true — OGX will orchestrate %d "
                    "platform.mcp entries server-side",
                    len(self.config.platform.mcp),
                )
        else:
            for mcp_cfg in self.config.mcp_servers:
                await self.connect_mcp(mcp_cfg)

        # 10. Seed messages with system prompt + optional memory prefix.
        self._append_message(
            {"role": "system", "content": self.build_system_prompt()}
        )
        prefix = await self.build_memory_prefix()
        if prefix:
            self._append_message(
                {"role": self.config.memory.prefix_role, "content": prefix}
            )
            logger.info(
                "Memory prefix injected (%d chars, role=%s)",
                len(prefix),
                self.config.memory.prefix_role,
            )

        # 11. Reasoning parser for models that use <think> tags in content.
        self._reasoning_parser: ThinkTagParser | None = create_reasoning_parser(
            self.config.model.name
        )
        if self._reasoning_parser:
            logger.info(
                "Think-tag reasoning parser enabled for model %s",
                self.config.model.name,
            )

        self._discover_capabilities()

        self._setup_done = True
        logger.info("Agent setup complete")

        await self._fire_setup_hooks(base)

    async def _fire_setup_hooks(self, base: Path) -> None:
        """Fire ``setup_complete`` hooks and inject stdout as context."""
        if not self.hooks:
            return
        env_extra = {
            "AGENT_NAME": self.config.agent.name,
            "AGENT_PROJECT_DIR": str(base.resolve()),
        }
        try:
            results = await self.hooks.fire(
                "setup_complete", env_extra=env_extra, cwd=base.resolve(),
            )
        except Exception:
            logger.warning("setup_complete hooks failed", exc_info=True)
            return
        for result in results:
            if result.success and result.stdout:
                content = result.stdout
                limit = self.config.memory.max_prefix_chars
                if limit and len(content) > limit:
                    content = content[:limit] + "\n\n… [truncated]"
                self._append_message({
                    "role": self.config.memory.prefix_role,
                    "content": content,
                })
                logger.info(
                    "setup_complete hook injected %d chars (hook=%s)",
                    len(content),
                    result.hook.name or result.hook.command[:40],
                )

    async def _refresh_mcp_auth(self, client_ref: _McpClientRef) -> bool:
        """Fire ``mcp_auth_refresh`` hook, reconnect with fresh headers.

        Called automatically when an MCP tool call receives a 401/403.
        The hook script can refresh tokens and print ``KEY=VALUE`` lines
        to stdout — those are injected into ``os.environ`` so that
        subsequent ``substitute_env_vars()`` calls pick them up.

        Returns ``True`` if the reconnect succeeded.
        """
        async with client_ref._reconnect_lock:
            label = client_ref.label
            config = client_ref.config

            # Let hooks refresh tokens / env vars.
            if self.hooks:
                _base = self._base_dir or self._config_path.parent
                results = await self.hooks.fire(
                    "mcp_auth_refresh",
                    env_extra={
                        "AGENT_NAME": self.config.agent.name,
                        "AGENT_PROJECT_DIR": str(_base.resolve()),
                        "MCP_SERVER_URL": label,
                    },
                    cwd=_base.resolve(),
                )
                for r in results:
                    if r.success and r.stdout:
                        for line in r.stdout.strip().splitlines():
                            if "=" in line:
                                key, _, value = line.partition("=")
                                _os.environ[key.strip()] = value.strip()

            # Close old client.
            old_client = client_ref.client
            try:
                if hasattr(old_client, "__aexit__"):
                    await old_client.__aexit__(None, None, None)
                elif hasattr(old_client, "close"):
                    await old_client.close()
            except Exception:
                logger.debug("Error closing old MCP client %s", label, exc_info=True)

            # Rebuild headers from templates (if available).
            new_headers = None
            if client_ref.header_templates:
                from fipsagents.baseagent.config import substitute_env_vars
                new_headers = {
                    k: substitute_env_vars(v)
                    for k, v in client_ref.header_templates.items()
                }
            elif config and config.headers:
                new_headers = config.headers

            # Reconnect.
            try:
                if config and config.url:
                    if new_headers:
                        from fastmcp.client.transports import StreamableHttpTransport
                        transport = StreamableHttpTransport(
                            url=config.url, headers=new_headers,
                        )
                    else:
                        transport = config.url
                else:
                    logger.warning(
                        "Cannot reconnect MCP server %s — no HTTP config", label,
                    )
                    return False

                from fastmcp import Client as McpClient
                new_client = McpClient(transport)
                await new_client.__aenter__()

                client_ref.client = new_client
                if config and new_headers:
                    config.headers = new_headers

                logger.info("MCP server %s reconnected with refreshed auth", label)
                return True
            except Exception:
                logger.exception("Failed to reconnect MCP server %s", label)
                return False

    async def run(self) -> Any:
        """Execute the agent loop until DONE or max iterations."""
        if not self._setup_done:
            raise RuntimeError(
                "Agent.run() called before setup(). Call setup() first, "
                "or use start() for the full lifecycle."
            )

        max_iter = self.config.loop.max_iterations
        backoff_cfg = self.config.loop.backoff
        consecutive_errors = 0

        for iteration in range(1, max_iter + 1):
            logger.debug("Step %d/%d", iteration, max_iter)

            try:
                result = await self.step()
            except Exception:
                consecutive_errors += 1
                delay = min(
                    backoff_cfg.initial * (backoff_cfg.multiplier ** (consecutive_errors - 1)),
                    backoff_cfg.max,
                )
                logger.exception(
                    "Step %d raised an exception — backing off %.1fs "
                    "(consecutive errors: %d)",
                    iteration,
                    delay,
                    consecutive_errors,
                )
                await asyncio.sleep(delay)
                continue

            # Reset error counter on a successful step.
            consecutive_errors = 0

            if result.outcome is StepOutcome.DONE:
                logger.info(
                    "Agent completed after %d step(s)", iteration
                )
                return result.result

        logger.warning(
            "Agent hit max iterations (%d) without completing", max_iter
        )
        return None

    async def shutdown(self) -> None:
        """Clean up resources: close MCP connections and any open handles."""
        logger.info("Shutting down agent")
        if self.hooks and self._setup_done:
            base = self._base_dir or self._config_path.parent
            try:
                await self.hooks.fire(
                    "shutdown",
                    env_extra={
                        "AGENT_NAME": getattr(
                            getattr(self.config, "agent", None), "name", ""
                        ),
                        "AGENT_PROJECT_DIR": str(base.resolve()),
                    },
                    cwd=base.resolve(),
                )
            except Exception:
                logger.warning("Shutdown hooks failed", exc_info=True)
        for ref in self._mcp_clients:
            try:
                if hasattr(ref.client, "close"):
                    await ref.client.close()
                elif hasattr(ref.client, "disconnect"):
                    await ref.client.disconnect()
            except Exception:
                logger.warning(
                    "Error closing MCP client", exc_info=True
                )
        self._mcp_clients.clear()
        self._mcp_prompts.clear()
        self._mcp_resources.clear()
        self._mcp_resource_templates.clear()
        self._setup_done = False
        logger.info("Agent shutdown complete")

    async def start(self) -> Any:
        """Full lifecycle: setup -> run -> shutdown (with guaranteed cleanup)."""
        try:
            await self.setup()
            return await self.run()
        finally:
            await self.shutdown()

    # -- Reducer hooks -------------------------------------------------------

    def reduce(self, state: Any, event: StreamEvent) -> Any:
        """Pure synchronous reducer: ``(state, event) -> state``.

        Override in subclasses to evolve state in response to events.
        Must be pure: no I/O, no side effects, no mutation of *state*.
        """
        return state

    async def after_event(self, state: Any, event: StreamEvent) -> None:
        """Async side-effect hook.  Called for new events only, never
        during replay.  Override to trigger notifications, external
        API calls, or other effects that should happen exactly once.
        """

    # -- Step: one iteration of agent logic ---------------------------------

    async def step(self) -> StepResult:
        """One iteration of agent logic.

        The default implementation consumes :meth:`astep_stream` and returns
        the concatenated ``ContentDelta`` content as a ``StepResult.done``.
        Subclasses typically override ``astep_stream`` only; both sync and
        streaming clients then share the same ReAct loop, tool dispatch, and
        any pre/post-turn hooks (memory recall, system prompt injection).

        Override this method directly only when a subclass needs sync-specific
        behavior that doesn't make sense to expose as events — most agents
        should not.
        """
        content_parts: list[str] = []
        async for event in self.astep_stream():
            if isinstance(event, ContentDelta):
                content_parts.append(event.content)
        return StepResult.done("".join(content_parts))

    # -- Conversation state --------------------------------------------------

    def _append_message(self, msg: dict[str, Any]) -> None:
        """Append *msg* to ``self.messages`` with a stable ID stamped."""
        _stamp_message_id(msg)
        self.messages.append(msg)

    def add_message(self, role: str, content: str | list[dict[str, Any]]) -> None:
        """Append a message to the conversation history.

        ``content`` accepts either a plain string or a list of OpenAI-shaped
        content blocks (e.g. ``{"type": "text", "text": ...}`` /
        ``{"type": "image_url", "image_url": {...}}``) so multimodal callers
        can append image-bearing turns without going through the HTTP layer.
        """
        msg = {"role": role, "content": content}
        self._append_message(msg)

    def get_messages(self) -> list[dict[str, Any]]:
        """Return a copy of the current conversation history."""
        return list(self.messages)

    def clear_messages(self) -> None:
        """Reset the conversation history."""
        self.messages.clear()

    # -- LLM convenience methods ---------------------------------------------
    # These delegate to self.llm but automatically include conversation state
    # and tool schemas when appropriate.

    async def call_model(
        self,
        messages: list[dict[str, Any]] | None = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        include_tools: bool = True,
        **kwargs: Any,
    ) -> ModelResponse:
        """Chat completion.  Defaults to ``self.messages`` and auto-includes
        LLM-visible tool schemas unless *include_tools* is ``False``."""
        self._require_llm()
        msgs = messages if messages is not None else self.messages
        if include_tools and tools is None:
            schemas = self.get_tool_schemas()
            tools = schemas if schemas else None
        return await self.llm.call_model(msgs, tools=tools, **kwargs)

    async def call_model_json(
        self,
        schema: Any,
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Structured-output completion.  Returns parsed/validated object."""
        self._require_llm()
        msgs = messages if messages is not None else self.messages
        return await self.llm.call_model_json(msgs, schema, **kwargs)

    async def call_model_stream(
        self,
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Streaming completion.  Yields content chunks."""
        self._require_llm()
        msgs = messages if messages is not None else self.messages
        async for chunk in self.llm.call_model_stream(msgs, **kwargs):
            yield chunk

    async def astep_stream(
        self,
        *,
        max_iterations: int = 10,
        include_tools: bool | None = None,
        **model_kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Streaming agent loop. Yields typed ``StreamEvent`` values.

        Drives the model in streaming mode and emits:

        - ``ReasoningDelta`` for each ``delta.reasoning_content`` chunk
          (models like gpt-oss-20b expose this natively)
        - ``ToolCallDelta`` for each incremental tool-call chunk the
          model emits, including ``arguments`` streamed token-by-token
        - ``ToolResultEvent`` after the agent executes each tool
        - ``ContentDelta`` for each ``delta.content`` chunk (the
          user-visible response)
        - ``StreamComplete`` as the terminal event, carrying
          ``StreamMetrics`` (TTFT, ITL samples, totals)

        The loop terminates when the model returns a turn with
        ``finish_reason`` other than ``"tool_calls"``. Subclasses that
        want custom pre/post-turn work (memory recall, message
        injection) should override this method and call ``super()``.

        This is source-agnostic: tools from MCP servers and local
        ``@tool`` functions flow through the same dispatch point, so
        streaming looks identical regardless of tool origin.

        ``include_tools`` is a per-call override controlling whether
        registered tool schemas are emitted to the upstream model.
        ``None`` (the default) honors ``config.tools.enabled``;
        ``True``/``False`` overrides the config for this call only.  Set
        ``False`` for vLLM checkpoints that 400 on tool schemas
        (vision-only, voice-only).  Mirrors the ``include_tools`` flag
        on :meth:`call_model`.
        """
        self._require_llm()

        # Reset per-turn headroom flag so we warn at most once per turn.
        self._headroom_warned = False

        # Deferred memory injection — runs before the first model call.
        await self._inject_deferred_memory()

        metrics = StreamMetrics()

        # Per-turn limit config — resolved once, checked after each model call.
        _limits_cfg = getattr(getattr(self, "config", None), "model", None)
        _limits = getattr(_limits_cfg, "limits", None) if _limits_cfg else None
        _cumulative_prompt = 0
        _cumulative_completion = 0
        _loop_broken = False

        # Doom-loop guard config — tracks repeated tool calls.
        _guard_cfg = getattr(getattr(getattr(self, "config", None), "loop", None), "guard", None)
        _guard_enabled = _guard_cfg is not None and getattr(_guard_cfg, "enabled", True)
        _guard_window: list[str] = []
        _loop_guard_broken = False

        loop = asyncio.get_running_loop()
        start_time = loop.time()
        last_content_time: float | None = None

        def _mark_first_reasoning() -> None:
            if metrics.time_to_first_reasoning is None:
                metrics.time_to_first_reasoning = loop.time() - start_time

        def _mark_content(now: float) -> None:
            nonlocal last_content_time
            if metrics.time_to_first_content is None:
                metrics.time_to_first_content = now - start_time
            if last_content_time is not None:
                metrics.inter_token_latencies.append(now - last_content_time)
            last_content_time = now

        finish_reason = "stop"

        # Resolve once per call: explicit kwarg wins, else honor config,
        # else default True for backward compatibility when self.config
        # has not been initialized (eg test stubs that bypass setup()).
        if include_tools is None:
            cfg = getattr(self, "config", None)
            tools_active = cfg.tools.enabled if cfg is not None else True
        else:
            tools_active = include_tools

        for _ in range(max_iterations):
            metrics.model_calls += 1
            schemas = self.get_tool_schemas() if tools_active else []
            tools_arg = schemas if schemas else None

            # Accumulators for this turn. Keyed by tool_call index since
            # OpenAI streams multiple concurrent tool calls interleaved.
            tool_buf: dict[int, dict[str, Any]] = {}
            assistant_content_parts: list[str] = []
            if self._reasoning_parser:
                self._reasoning_parser.reset()

            async for chunk in self.llm.call_model_stream_raw(
                self.messages, tools=tools_arg, **model_kwargs
            ):
                try:
                    choice = chunk.choices[0]
                except (AttributeError, IndexError):
                    continue
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                # Reasoning ("thinking") deltas.
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    _mark_first_reasoning()
                    yield ReasoningDelta(content=reasoning)

                # Content deltas. If a reasoning parser is active,
                # separate <think>…</think> blocks from visible content.
                content = getattr(delta, "content", None)
                if content:
                    if self._reasoning_parser:
                        for kind, text in self._reasoning_parser.feed(content):
                            if kind == "reasoning":
                                _mark_first_reasoning()
                                yield ReasoningDelta(content=text)
                            else:
                                now = loop.time()
                                _mark_content(now)
                                assistant_content_parts.append(text)
                                yield ContentDelta(content=text)
                    else:
                        now = loop.time()
                        _mark_content(now)
                        assistant_content_parts.append(content)
                        yield ContentDelta(content=content)

                # Tool-call deltas. OpenAI streams these with an
                # ``index`` so concurrent calls stay distinct.
                tc_list = getattr(delta, "tool_calls", None) or []
                for tc in tc_list:
                    idx = getattr(tc, "index", 0) or 0
                    is_new = idx not in tool_buf
                    buf = tool_buf.setdefault(
                        idx, {"id": None, "name": None, "arguments": ""}
                    )
                    tc_id = getattr(tc, "id", None)
                    fn = getattr(tc, "function", None)
                    tc_name = getattr(fn, "name", None) if fn else None
                    tc_args = getattr(fn, "arguments", None) if fn else None

                    if tc_id and not buf["id"]:
                        buf["id"] = tc_id
                    if tc_name and not buf["name"]:
                        buf["name"] = tc_name
                    if tc_args:
                        buf["arguments"] += tc_args

                    # Emit an opening chunk (with id + name) for the first
                    # delta of each tool call.  If the provider didn't send
                    # an id on the first chunk, generate a synthetic one so
                    # downstream SSE serialization always has something to key on.
                    if is_new:
                        if not buf["id"]:
                            import uuid as _uuid
                            buf["id"] = f"chatcmpl-tool-{_uuid.uuid4().hex[:16]}"
                        yield ToolCallDelta(
                            index=idx,
                            call_id=buf["id"],
                            name=buf["name"] or "",
                            arguments_delta=tc_args or "",
                        )
                    else:
                        yield ToolCallDelta(
                            index=idx,
                            call_id=None,
                            name=None,
                            arguments_delta=tc_args or "",
                        )

                turn_finish = getattr(choice, "finish_reason", None)
                if turn_finish:
                    finish_reason = turn_finish

            # Flush any buffered reasoning parser state.
            if self._reasoning_parser:
                for kind, text in self._reasoning_parser.flush():
                    if kind == "reasoning":
                        yield ReasoningDelta(content=text)
                    else:
                        assistant_content_parts.append(text)
                        yield ContentDelta(content=text)

            # Extract any usage stats the provider reported. Not all
            # providers send these with streaming.
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                pt = getattr(usage, "prompt_tokens", None)
                ct = getattr(usage, "completion_tokens", None)
                tt = getattr(usage, "total_tokens", None)
                if pt is not None:
                    metrics.prompt_tokens = pt
                if ct is not None:
                    metrics.completion_tokens = ct
                if tt is not None:
                    metrics.total_tokens = tt

            # Accumulate tokens for per-turn limit checks.
            if usage is not None:
                _cumulative_prompt += int(getattr(usage, "prompt_tokens", 0) or 0)
                _cumulative_completion += int(getattr(usage, "completion_tokens", 0) or 0)

            # Per-turn limit checks (before tool dispatch).
            if _limits is not None:
                _exceeded = None
                _cum_total = _cumulative_prompt + _cumulative_completion

                if (
                    _limits.max_tokens_per_turn is not None
                    and _cum_total > _limits.max_tokens_per_turn
                ):
                    _exceeded = LimitExceeded(
                        limit_type="tokens",
                        threshold=float(_limits.max_tokens_per_turn),
                        actual=float(_cum_total),
                    )
                elif (
                    _limits.max_iterations_per_turn is not None
                    and metrics.model_calls >= _limits.max_iterations_per_turn
                ):
                    _exceeded = LimitExceeded(
                        limit_type="iterations",
                        threshold=float(_limits.max_iterations_per_turn),
                        actual=float(metrics.model_calls),
                    )
                elif _limits.max_cost_per_turn_usd is not None:
                    _turn_cost = compute_cost(
                        self.config.model.name,
                        input_tokens=_cumulative_prompt,
                        output_tokens=_cumulative_completion,
                        pricing=self.config.pricing,
                    )
                    if _turn_cost > _limits.max_cost_per_turn_usd:
                        _exceeded = LimitExceeded(
                            limit_type="cost",
                            threshold=float(_limits.max_cost_per_turn_usd),
                            actual=float(_turn_cost),
                        )

                if _exceeded is not None:
                    _limit_audit = logging.getLogger(
                        "fipsagents.security.audit.limits"
                    )
                    _limit_audit.warning(
                        "limit_exceeded type=%s threshold=%s actual=%s",
                        _exceeded.limit_type,
                        _exceeded.threshold,
                        _exceeded.actual,
                    )
                    yield _exceeded
                    finish_reason = "limit"
                    _loop_broken = True
                    break

            # Budget headroom check for checked-out work items.
            # Use getattr defensively for agents constructed via __new__
            # in tests that bypass __init__.
            _wi = getattr(self, "_checked_out_work_item", None)
            if not _loop_broken and _wi is not None:
                if _wi.max_cost_usd is not None and _wi.max_cost_usd > 0:
                    _wi_cfg = getattr(
                        getattr(getattr(self, "config", None), "server", None),
                        "work_items", None,
                    )
                    _headroom_pct = (
                        _wi_cfg.budget_headroom_pct
                        if _wi_cfg is not None
                        else 10.0
                    )
                    _wi_turn_cost = compute_cost(
                        self.config.model.name,
                        input_tokens=_cumulative_prompt,
                        output_tokens=_cumulative_completion,
                        pricing=self.config.pricing,
                    )
                    _remaining = _wi.max_cost_usd - _wi_turn_cost
                    _threshold = _wi.max_cost_usd * (_headroom_pct / 100.0)
                    if _remaining <= _threshold and not getattr(
                        self, "_headroom_warned", False
                    ):
                        yield BudgetHeadroomWarning(
                            item_id=_wi.id,
                            remaining_pct=max(
                                0.0,
                                (_remaining / _wi.max_cost_usd) * 100.0,
                            ),
                        )
                        self._append_message({
                            "role": "system",
                            "content": (
                                f"Budget headroom warning: ${_remaining:.4f} "
                                f"remaining of ${_wi.max_cost_usd:.4f} for "
                                f"work item {_wi.id!r}. Complete or release "
                                f"the work item now."
                            ),
                        })
                        self._headroom_warned = True
                        logger.warning(
                            "Budget headroom reached for work item %s: "
                            "%.1f%% remaining (threshold: %.1f%%)",
                            _wi.id,
                            max(
                                0.0,
                                (_remaining / _wi.max_cost_usd) * 100.0,
                            ),
                            _headroom_pct,
                        )

            # If the model decided to call tools, execute them and loop.
            if tool_buf:
                import json as _json

                assembled_calls = []
                for idx in sorted(tool_buf.keys()):
                    buf = tool_buf[idx]
                    if not buf["id"] or not buf["name"]:
                        continue
                    assembled_calls.append(
                        {
                            "id": buf["id"],
                            "type": "function",
                            "function": {
                                "name": buf["name"],
                                "arguments": buf["arguments"],
                            },
                        }
                    )

                # Append the assistant's tool-calling message so the
                # conversation history is correctly shaped for the next
                # model call.
                self._append_message(
                    {
                        "role": "assistant",
                        "content": "".join(assistant_content_parts) or None,
                        "tool_calls": assembled_calls,
                    }
                )

                # Execute each tool and emit the result event.
                for call in assembled_calls:
                    metrics.tool_calls += 1
                    fn_name = call["function"]["name"]
                    try:
                        args = (
                            _json.loads(call["function"]["arguments"])
                            if call["function"]["arguments"]
                            else {}
                        )
                    except _json.JSONDecodeError:
                        args = {}

                    # Tool-level approval check (@tool requires_approval).
                    # Runs before the server-layer permission source so
                    # per-tool metadata takes precedence over policy rules.
                    _preapproved = getattr(self, "_permission_preapproved", set())
                    _tool_meta = self.tools.get(fn_name)
                    _approval_handled = False
                    if (
                        _tool_meta is not None
                        and _tool_meta.requires_approval
                        and call["id"] not in _preapproved
                    ):
                        _needs_approval = True
                        if callable(_tool_meta.requires_approval):
                            if asyncio.iscoroutinefunction(_tool_meta.requires_approval):
                                _needs_approval = await _tool_meta.requires_approval(**args)
                            else:
                                _needs_approval = _tool_meta.requires_approval(**args)

                        if _needs_approval:
                            import json as _approval_json
                            _approval_q_id = _generate_message_id().replace(
                                "msg_", "perm_"
                            )
                            _approval_pending = {
                                "question_id": _approval_q_id,
                                "prompt": (
                                    f"Tool '{fn_name}' requires approval. "
                                    f"Arguments: {args}. Approve?"
                                ),
                                "options": [
                                    {"label": "Allow", "value": "allow"},
                                    {"label": "Deny", "value": "deny"},
                                ],
                                "multiple": False,
                                "allow_custom": False,
                                "permission_ask": True,
                                "tool_name": fn_name,
                                "tool_args": args,
                                "tool_call_id": call["id"],
                            }
                            self._question_pending = _approval_pending

                            yield QuestionAsked(
                                question_id=_approval_q_id,
                                question_text=_approval_pending["prompt"],
                                options=_approval_pending["options"],
                                multiple=False,
                                allow_custom=False,
                            )

                            _sentinel = _approval_json.dumps({
                                "__permission_pending__": True,
                                "question_id": _approval_q_id,
                                "tool_name": fn_name,
                            })
                            self._append_message({
                                "role": "tool",
                                "content": _sentinel,
                                "tool_call_id": call["id"],
                            })
                            yield ToolResultEvent(
                                call_id=call["id"],
                                name=fn_name,
                                content=_sentinel,
                                is_error=False,
                            )

                            self._question_pending["tool_call_id"] = call["id"]
                            finish_reason = "question"
                            _approval_handled = True
                            break

                    # Permission check (before tool execution).
                    _perm_src = getattr(self, "_permission_source", None)
                    if not _approval_handled and _perm_src is not None:
                        _perm_mode = getattr(self, "_permission_mode", "enforce")

                        if call["id"] not in _preapproved:
                            _perm_ctx = {"args": args, "tool_call_id": call["id"]}
                            _perm_decision = await _perm_src.resolve(
                                fn_name, context=_perm_ctx,
                            )

                            yield PermissionDecisionMade(
                                tool=fn_name,
                                action=_perm_decision.action,
                                rule_id=_perm_decision.rule_id,
                                scope=_perm_decision.scope,
                            )

                            _perm_audit = logging.getLogger(
                                "fipsagents.security.audit.permissions"
                            )
                            _perm_audit.info(
                                "permission_decision tool=%s action=%s "
                                "rule_id=%s mode=%s",
                                fn_name, _perm_decision.action,
                                _perm_decision.rule_id, _perm_mode,
                            )

                            if _perm_mode == "enforce":
                                if _perm_decision.action == "deny":
                                    _deny_msg = (
                                        f"DENIED: Tool '{fn_name}' is not "
                                        f"permitted. "
                                        f"{_perm_decision.reason or 'Permission denied by policy.'}"
                                    )
                                    self._append_message({
                                        "role": "tool",
                                        "content": _deny_msg,
                                        "tool_call_id": call["id"],
                                    })
                                    yield ToolResultEvent(
                                        call_id=call["id"],
                                        name=fn_name,
                                        content=_deny_msg,
                                        is_error=True,
                                    )
                                    continue

                                if _perm_decision.action == "ask":
                                    import json as _perm_json
                                    _perm_q_id = _generate_message_id().replace(
                                        "msg_", "perm_"
                                    )
                                    _perm_pending = {
                                        "question_id": _perm_q_id,
                                        "prompt": (
                                            f"Tool '{fn_name}' requires "
                                            f"approval. "
                                            f"{_perm_decision.reason or ''}"
                                        ).strip(),
                                        "options": [
                                            {"label": "Allow", "value": "allow"},
                                            {"label": "Deny", "value": "deny"},
                                        ],
                                        "multiple": False,
                                        "allow_custom": False,
                                        "permission_ask": True,
                                        "tool_name": fn_name,
                                        "tool_args": args,
                                        "tool_call_id": call["id"],
                                        "rule_id": _perm_decision.rule_id,
                                    }
                                    self._question_pending = _perm_pending

                                    yield QuestionAsked(
                                        question_id=_perm_q_id,
                                        question_text=_perm_pending["prompt"],
                                        options=_perm_pending["options"],
                                        multiple=False,
                                        allow_custom=False,
                                    )

                                    _sentinel = _perm_json.dumps({
                                        "__permission_pending__": True,
                                        "question_id": _perm_q_id,
                                        "tool_name": fn_name,
                                    })
                                    self._append_message({
                                        "role": "tool",
                                        "content": _sentinel,
                                        "tool_call_id": call["id"],
                                    })
                                    yield ToolResultEvent(
                                        call_id=call["id"],
                                        name=fn_name,
                                        content=_sentinel,
                                        is_error=False,
                                    )

                                    self._question_pending["tool_call_id"] = call["id"]
                                    finish_reason = "question"
                                    break

                    # Pre-tool hook: can block execution via non-zero exit.
                    _hooks = getattr(self, "hooks", None)
                    if not _approval_handled and _hooks:
                        import json as _hook_json
                        _base = self._base_dir or self._config_path.parent
                        _pre_results = await _hooks.fire(
                            "pre_tool_use",
                            env_extra={
                                "AGENT_NAME": self.config.agent.name,
                                "AGENT_PROJECT_DIR": str(_base.resolve()),
                                "TOOL_NAME": fn_name,
                                "TOOL_ARGS": _hook_json.dumps(args, default=str),
                            },
                            cwd=_base.resolve(),
                            tool_name=fn_name,
                        )
                        _blocked = [r for r in _pre_results if r.blocked]
                        if _blocked:
                            _block_reason = (
                                _blocked[0].stderr or _blocked[0].stdout
                                or "non-zero exit"
                            )
                            _block_msg = (
                                f"BLOCKED by pre_tool_use hook: {_block_reason}"
                            )
                            self._append_message({
                                "role": "tool",
                                "content": _block_msg,
                                "tool_call_id": call["id"],
                            })
                            yield ToolResultEvent(
                                call_id=call["id"],
                                name=fn_name,
                                content=_block_msg,
                                is_error=True,
                            )
                            continue

                    result = await self.tools.execute(fn_name, args)

                    # Drain subagent events emitted by delegate_to_agent
                    # (or any future tool that uses _subagent_events).
                    # Yield in append order: SubagentInvoked → SubagentCompleted/Failed
                    # before the ToolResultEvent so consumers see the full
                    # lifecycle in the order the design doc's diagram implies.
                    # Use getattr defensively for agents constructed via __new__
                    # in tests that bypass __init__.
                    _pending = getattr(self, "_subagent_events", None)
                    while _pending:
                        yield _pending.pop(0)

                    # Drain question events emitted by ask_user.
                    _q_events = getattr(self, "_question_events", None)
                    while _q_events:
                        yield _q_events.pop(0)

                    # Drain work-item events.
                    _wi_events = getattr(self, "_work_item_events", None)
                    while _wi_events:
                        yield _wi_events.pop(0)

                    # Drain self-healing events.
                    _sh_events = getattr(self, "_self_healing_events", None)
                    while _sh_events:
                        yield _sh_events.pop(0)

                    is_err = result.is_error
                    content_str = (
                        result.result
                        if not is_err
                        else f"ERROR: {result.error}"
                    )

                    self._append_message(
                        {
                            "role": "tool",
                            "content": content_str,
                            "tool_call_id": call["id"],
                        }
                    )
                    yield ToolResultEvent(
                        call_id=call["id"],
                        name=fn_name,
                        content=content_str,
                        is_error=is_err,
                    )

                    # Post-tool hook (informational, cannot block).
                    if _hooks:
                        import json as _post_hook_json
                        _base = self._base_dir or self._config_path.parent
                        await _hooks.fire(
                            "post_tool_use",
                            env_extra={
                                "AGENT_NAME": self.config.agent.name,
                                "AGENT_PROJECT_DIR": str(_base.resolve()),
                                "TOOL_NAME": fn_name,
                                "TOOL_ARGS": _post_hook_json.dumps(args, default=str),
                                "TOOL_RESULT": content_str[:4096],
                            },
                            cwd=_base.resolve(),
                            tool_name=fn_name,
                        )

                    # Doom-loop guard: hash (tool_name, args) and check for repeats.
                    if _guard_enabled and _guard_cfg is not None:
                        _call_hash = _doom_loop_hash(fn_name, args, _guard_cfg.canonicalization)
                        _guard_window.append(_call_hash)
                        if len(_guard_window) > _guard_cfg.pattern_window:
                            _guard_window = _guard_window[-_guard_cfg.pattern_window:]
                        _repeat_count = _guard_window.count(_call_hash)
                        if _repeat_count >= _guard_cfg.repeat_threshold:
                            _guard_audit = logging.getLogger("fipsagents.security.audit.loop_guard")
                            _guard_audit.warning(
                                "doom_loop_detected tool=%s repeat_count=%d args=%s",
                                fn_name, _repeat_count, _truncate(repr(args), 200),
                            )
                            yield LoopBreakEvent(
                                tool_name=fn_name,
                                repeat_count=_repeat_count,
                                last_args=args,
                                last_error=content_str if is_err else None,
                            )
                            finish_reason = "loop_break"
                            _loop_guard_broken = True
                            break

                    # If ask_user set a pending question, stamp the
                    # tool_call_id and stop the loop.
                    _q_state = getattr(self, "_question_pending", None)
                    if _q_state is not None:
                        _q_state["tool_call_id"] = call["id"]
                        finish_reason = "question"
                        break

                # If a question is pending, break the outer loop.
                if getattr(self, "_question_pending", None) is not None:
                    break

                if _loop_guard_broken:
                    break

                # Continue the loop: call the model again with the tool
                # results appended.
                continue

            # No tool calls -> this turn produced the final response.
            if assistant_content_parts:
                self._append_message(
                    {
                        "role": "assistant",
                        "content": "".join(assistant_content_parts),
                    }
                )
            break
        else:
            # Loop exhausted without break -> hit iteration cap.
            finish_reason = "length"

        metrics.total_time = loop.time() - start_time
        yield StreamComplete(finish_reason=finish_reason, metrics=metrics)

    async def call_model_validated(
        self,
        validator_fn: Callable[[ModelResponse], T],
        messages: list[dict[str, Any]] | None = None,
        *,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> T:
        """Call model, validate response, retry with backoff on failure."""
        self._require_llm()
        msgs = messages if messages is not None else self.messages
        return await self.llm.call_model_validated(
            msgs, validator_fn, max_retries=max_retries, **kwargs
        )

    def _require_llm(self) -> None:
        """Guard against calling LLM methods before setup."""
        if self.llm is None:
            raise RuntimeError(
                "LLM client not initialised. Call setup() before making "
                "model calls."
            )

    # -- Platform mode (Responses API + moderations) -----------------------

    async def call_model_responses(
        self,
        input: str | list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        guardrails: list[str] | None = None,
        **kwargs: Any,
    ) -> "PlatformResponse":
        """Non-streaming OGX ``/v1/responses`` call.

        Requires ``platform.enabled=true`` in ``agent.yaml``.  Defaults
        ``tools`` to the configured ``platform.mcp`` list and
        ``guardrails`` to ``platform.guardrails``; either can be
        overridden per-call.  See :meth:`LLMClient.call_model_responses`
        for full semantics.
        """
        self._require_llm()
        return await self.llm.call_model_responses(
            input, tools=tools, guardrails=guardrails, **kwargs
        )

    async def call_model_responses_stream(
        self,
        input: str | list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        guardrails: list[str] | None = None,
        **kwargs: Any,
    ) -> "AsyncIterator[StreamEvent]":
        """Streaming OGX ``/v1/responses`` call mapped to :class:`StreamEvent`.

        See :meth:`LLMClient.call_model_responses_stream`.  When a
        guardrail fires, emits :class:`GuardrailFiredEvent` followed by
        :class:`StreamComplete` with ``finish_reason="guardrail"``.
        """
        self._require_llm()
        async for event in self.llm.call_model_responses_stream(
            input, tools=tools, guardrails=guardrails, **kwargs
        ):
            yield event

    async def moderate(
        self,
        content: str,
        *,
        model: str | None = None,
    ) -> "ModerationResult":
        """Classify *content* via OGX ``/v1/moderations``.

        Observability-only — never blocks.  When ``model`` is omitted,
        defaults to the first entry in ``platform.guardrails`` (in OGX,
        shield ids and moderation model ids share a namespace).  Emits
        a structured log line for each call so deployments without a
        trace exporter still get the audit trail.
        """
        self._require_llm()
        if model is None:
            shields = list(self.config.platform.guardrails)
            if not shields:
                raise ValueError(
                    "moderate() called without `model` and "
                    "platform.guardrails is empty — nothing to classify with."
                )
            model = shields[0]
        result = await self.llm.moderate(content, model=model)
        logger.info(
            "moderation: model=%s flagged=%s categories=%s",
            result.model,
            result.flagged,
            sorted(k for k, v in result.categories.items() if v),
        )
        return result

    # -- Tool dispatch -------------------------------------------------------

    async def use_tool(self, tool_name: str, args: dict[str, Any] | None = None) -> ToolResult:
        """Call a tool through the registry.

        This is the single dispatch point for all agent-code tool calls
        (plane 1).  Logging is applied around the call.
        """
        args = args or {}
        logger.info("Tool call: %s(%s)", tool_name, _summarise_kwargs(args))
        result = await self.tools.execute(tool_name, args)
        if result.is_error:
            logger.warning("Tool %s failed: %s", tool_name, result.error)
        else:
            logger.debug("Tool %s returned: %s", tool_name, _truncate(result.result))
        return result

    async def run_tool_calls(
        self, response: ModelResponse, *, max_rounds: int = 50,
    ) -> ModelResponse:
        """Execute LLM-initiated tool calls and return the final response.

        Drives the tool-call loop for the non-streaming path: appends the
        assistant's tool-calling message, executes each tool via
        :meth:`tools.execute`, appends tool-result messages, and calls the
        model again.  Repeats until the model stops issuing tool calls or
        *max_rounds* is reached.

        For the **streaming** path, :meth:`astep_stream` handles its own
        dispatch internally — do not use this method there.

        Parameters
        ----------
        response:
            The model response (from :meth:`call_model`) that may contain
            tool calls.
        max_rounds:
            Safety valve — maximum number of call-execute-call cycles.
            Prevents runaway loops if the model never stops calling tools.

        Returns
        -------
        ModelResponse
            The final model response with no pending tool calls.
        """
        import json as _json

        round_count = 0
        while response.tool_calls:
            round_count += 1
            if round_count > max_rounds:
                logger.warning(
                    "run_tool_calls hit max_rounds=%d — returning last response",
                    max_rounds,
                )
                break

            # Append assistant message with tool_calls to conversation history.
            self._append_message({
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in response.tool_calls
                ],
            })

            # Execute each tool and append result.
            for tc in response.tool_calls:
                fn_name = tc.function.name
                try:
                    args = (
                        _json.loads(tc.function.arguments)
                        if tc.function.arguments
                        else {}
                    )
                except _json.JSONDecodeError:
                    args = {}

                logger.info("run_tool_calls: executing %s", fn_name)
                result = await self.tools.execute(fn_name, args)
                content_str = (
                    result.result
                    if not result.is_error
                    else f"ERROR: {result.error}"
                )
                self._append_message({
                    "role": "tool",
                    "content": content_str,
                    "tool_call_id": tc.id,
                })

            response = await self.call_model()

        return response

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI-compatible tool schemas for LLM-visible tools."""
        return self.tools.generate_schemas()

    # -- MCP integration -----------------------------------------------------

    async def connect_mcp(
        self, target: Any,
    ) -> None:
        """Connect to an MCP server via FastMCP v3 and register its tools, prompts, and resources.

        Parameters
        ----------
        target:
            One of:

            - **str** — URL for HTTP transport (backward-compatible).
            - **McpServerConfig** — HTTP (``url``) or stdio (``command``).
            - **FastMCP** — in-process server object (no subprocess or
              network; FastMCP v3 ``FastMCPTransport``).
        """
        # Resolve the transport argument for the FastMCP Client.
        if isinstance(target, str):
            label = target
            transport: Any = target
        elif isinstance(target, McpServerConfig):
            if target.url:
                label = target.url
                if target.headers:
                    from fastmcp.client.transports import StreamableHttpTransport

                    transport = StreamableHttpTransport(
                        url=target.url,
                        headers=target.headers,
                    )
                else:
                    transport = target.url
            else:
                label = f"stdio:{target.command}"
                from fastmcp.client.transports import StdioTransport

                transport = StdioTransport(
                    command=target.command,
                    args=target.args,
                    env=target.env,
                    cwd=target.cwd,
                )
        else:
            # Assume it's a FastMCP server instance (or any object that
            # FastMCP Client can auto-detect as a transport).
            label = getattr(target, "name", None) or type(target).__name__
            transport = target

        # Pre-connect hook: token acquisition, validation, logging.
        # Stdout KEY=VALUE lines are parsed into os.environ (same as
        # mcp_auth_refresh) so a single hook script can handle both
        # initial token acquisition and mid-session refresh.
        if self.hooks:
            _base = self._base_dir or self._config_path.parent
            _pre_results = await self.hooks.fire(
                "pre_mcp_connect",
                env_extra={
                    "AGENT_NAME": self.config.agent.name,
                    "AGENT_PROJECT_DIR": str(_base.resolve()),
                    "MCP_SERVER_URL": label,
                },
                cwd=_base.resolve(),
            )
            for _r in _pre_results:
                if _r.success and _r.stdout:
                    for _line in _r.stdout.strip().splitlines():
                        if "=" in _line:
                            _k, _, _v = _line.partition("=")
                            _os.environ[_k.strip()] = _v.strip()

            # Re-resolve headers from templates if env vars were updated.
            if (
                isinstance(target, McpServerConfig)
                and getattr(target, "_header_templates", None)
            ):
                from fipsagents.baseagent.config import substitute_env_vars
                target.headers = {
                    k: substitute_env_vars(v)
                    for k, v in target._header_templates.items()
                }
                # Rebuild transport with fresh headers.
                if target.headers:
                    from fastmcp.client.transports import StreamableHttpTransport
                    transport = StreamableHttpTransport(
                        url=target.url,
                        headers=target.headers,
                    )

        logger.info("Connecting to MCP server: %s", label)
        try:
            from fastmcp import Client as McpClient

            client = McpClient(transport)
            await client.__aenter__()

            # Build the mutable client ref so tool closures can survive
            # a reconnect without re-registration.
            client_ref = _McpClientRef(
                client=client,
                label=label,
                config=target if isinstance(target, McpServerConfig) else None,
                header_templates=(
                    getattr(target, "_header_templates", None)
                    if isinstance(target, McpServerConfig) else None
                ),
            )

            # Discover tools from the server.
            tools_list = await client.list_tools()
            registered = 0
            for mcp_tool in tools_list:
                # Wrap MCP tool as a local callable and register it.
                _register_mcp_tool(
                    self.tools, client_ref, mcp_tool,
                    reconnect_fn=self._refresh_mcp_auth,
                )
                registered += 1

            # Discover prompts.
            prompt_count = 0
            try:
                prompts_list = await client.list_prompts()
                for mcp_prompt in prompts_list:
                    pname = mcp_prompt.name
                    if pname in self._mcp_prompts:
                        logger.warning(
                            "MCP prompt %r already registered — skipping duplicate from %s",
                            pname, label,
                        )
                        continue
                    self._mcp_prompts[pname] = (client_ref, mcp_prompt)
                    prompt_count += 1
            except Exception:
                logger.debug("MCP server %s does not expose prompts (or error listing them)", label, exc_info=True)

            # Discover resources.
            resource_count = 0
            try:
                resources_list = await client.list_resources()
                for mcp_resource in resources_list:
                    uri_str = str(mcp_resource.uri)
                    if uri_str in self._mcp_resources:
                        logger.warning(
                            "MCP resource %r already registered — skipping duplicate from %s",
                            uri_str, label,
                        )
                        continue
                    self._mcp_resources[uri_str] = (client_ref, mcp_resource)
                    resource_count += 1
            except Exception:
                logger.debug("MCP server %s does not expose resources (or error listing them)", label, exc_info=True)

            # Discover resource templates.
            template_count = 0
            try:
                templates_list = await client.list_resource_templates()
                for mcp_template in templates_list:
                    tpl_str = mcp_template.uriTemplate
                    if tpl_str in self._mcp_resource_templates:
                        logger.warning(
                            "MCP resource template %r already registered — skipping duplicate from %s",
                            tpl_str, label,
                        )
                        continue
                    self._mcp_resource_templates[tpl_str] = (client_ref, mcp_template)
                    template_count += 1
            except Exception:
                logger.debug("MCP server %s does not expose resource templates (or error listing them)", label, exc_info=True)

            self._mcp_clients.append(client_ref)
            logger.info(
                "Connected to MCP server %s — %d tool(s), %d prompt(s), %d resource(s), %d template(s)",
                label, registered, prompt_count, resource_count, template_count,
            )

            # Post-connect hook: audit, validate expected tools, etc.
            if self.hooks:
                _base = self._base_dir or self._config_path.parent
                await self.hooks.fire(
                    "post_mcp_connect",
                    env_extra={
                        "AGENT_NAME": self.config.agent.name,
                        "AGENT_PROJECT_DIR": str(_base.resolve()),
                        "MCP_SERVER_URL": label,
                        "MCP_TOOLS_COUNT": str(registered),
                        "MCP_PROMPTS_COUNT": str(prompt_count),
                        "MCP_RESOURCES_COUNT": str(resource_count),
                    },
                    cwd=_base.resolve(),
                )
        except ImportError:
            logger.warning(
                "fastmcp package not installed — cannot connect to MCP "
                "server %s. Install with: pip install fastmcp",
                label,
            )
        except Exception:
            logger.exception(
                "Failed to connect to MCP server: %s", label
            )

    async def get_mcp_prompt(
        self, name: str, arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Render an MCP prompt by name.

        Calls the originating MCP server's ``get_prompt`` method.

        Returns
        -------
        mcp.types.GetPromptResult
            Contains ``messages`` (list of PromptMessage) and optional
            ``description``.

        Raises
        ------
        KeyError
            If *name* is not a discovered MCP prompt.
        """
        if name not in self._mcp_prompts:
            raise KeyError(
                f"MCP prompt {name!r} not found. "
                f"Available: {sorted(self._mcp_prompts)}"
            )
        client_ref, _prompt_meta = self._mcp_prompts[name]
        return await client_ref.client.get_prompt(name, arguments=arguments)

    async def read_resource(self, uri: str) -> Any:
        """Read an MCP resource by URI.

        Calls the originating MCP server's ``read_resource`` method.

        Returns
        -------
        list[mcp.types.TextResourceContents | mcp.types.BlobResourceContents]

        Raises
        ------
        KeyError
            If *uri* is not a discovered MCP resource.
        """
        if uri not in self._mcp_resources:
            raise KeyError(
                f"MCP resource {uri!r} not found. "
                f"Available: {sorted(self._mcp_resources)}"
            )
        client_ref, _resource_meta = self._mcp_resources[uri]
        return await client_ref.client.read_resource(uri)

    def list_mcp_prompts(self) -> list[dict[str, Any]]:
        """Return metadata for all discovered MCP prompts."""
        result = []
        for name, (_client_ref, prompt) in sorted(self._mcp_prompts.items()):
            args = getattr(prompt, "arguments", None) or []
            entry: dict[str, Any] = {
                "name": prompt.name,
                "description": getattr(prompt, "description", None) or "",
                "arguments": [
                    {
                        "name": a.name,
                        "description": getattr(a, "description", None) or "",
                        "required": getattr(a, "required", None),
                    }
                    for a in args
                ],
            }
            result.append(entry)
        return result

    def list_mcp_resources(self) -> list[dict[str, Any]]:
        """Return metadata for all discovered MCP resources."""
        result = []
        for uri, (_client_ref, resource) in sorted(self._mcp_resources.items()):
            result.append({
                "uri": str(resource.uri),
                "name": resource.name,
                "description": getattr(resource, "description", None) or "",
                "mimeType": getattr(resource, "mimeType", None),
            })
        return result

    def list_mcp_resource_templates(self) -> list[dict[str, Any]]:
        """Return metadata for all discovered MCP resource templates."""
        result = []
        for tpl, (_client_ref, template) in sorted(self._mcp_resource_templates.items()):
            result.append({
                "uriTemplate": template.uriTemplate,
                "name": template.name,
                "description": getattr(template, "description", None) or "",
                "mimeType": getattr(template, "mimeType", None),
            })
        return result

    # -- Capability auto-discovery ---------------------------------------------

    def _discover_capabilities(self) -> None:
        """Scan loaded subsystems and build a discovered capability list.

        Called at the end of setup() to introspect MCP servers, skills,
        and tools into Capability objects for work-item matching.
        """
        from fipsagents.server.work_items import Capability

        caps: list[Capability] = []

        # MCP server capabilities.
        for ref in self._mcp_clients:
            caps.append(Capability(name=f"mcp:{ref.label}", value=1.0))

        # Skill capabilities.
        for skill_name in self.skills._skills:
            caps.append(Capability(name=f"skill:{skill_name}", value=1.0))

        # Explicit config capabilities (merge, don't duplicate).
        seen = {c.name for c in caps}
        wi_cfg = getattr(
            getattr(getattr(self, "config", None), "server", None),
            "work_items",
            None,
        )
        if wi_cfg is not None:
            for c in wi_cfg.capabilities:
                if c.name not in seen:
                    caps.append(Capability(name=c.name, value=c.value))
                    seen.add(c.name)

        self._discovered_capabilities = caps
        if caps:
            logger.info(
                "Discovered %d capabilities: %s",
                len(caps),
                [c.name for c in caps],
            )

    # -- System prompt assembly -----------------------------------------------

    def build_system_prompt(self) -> str:
        """Assemble system prompt from named layers (or legacy flat mode)."""
        if self._assembler is not None:
            return self._assembler.assemble()

        # --- Legacy path (backward compatible) ---
        sections: list[str] = []

        try:
            prompt_name = self.config.prompts.system if self.config else "system"
            system_prompt = self.prompts.get(prompt_name)
            sections.append(system_prompt.render())
        except PromptNotFoundError:
            logger.debug("No 'system' prompt found — skipping")

        rules_text = self.rules.get_combined_content()
        if rules_text:
            sections.append(rules_text)

        manifest = self.skills.get_manifest()
        if manifest:
            skill_lines = ["# Available Skills", ""]
            for entry in manifest:
                triggers = ", ".join(entry.triggers) if entry.triggers else "none"
                skill_lines.append(
                    f"- **{entry.name}**: {entry.description} "
                    f"(triggers: {triggers})"
                )
            sections.append("\n".join(skill_lines))

        return "\n\n---\n\n".join(sections)

    async def build_memory_prefix(self) -> str | None:
        """Return a stable memory block to inject after the system prompt.

        Called once during :meth:`setup`.  The result is inserted as a
        message (role controlled by ``config.memory.prefix_role``) at
        index 1 in ``self.messages``, immediately after the system prompt.
        It stays pinned there for the life of the session — never
        re-queried per turn — so inference-server prefix caches stay warm.

        The default implementation reads the loading pattern from
        ``.memoryhub.yaml``.  For the ``eager`` pattern it retrieves the
        project's weight-ordered working set via
        ``search(query="", project_id=..., mode="index")``.  For deferred
        patterns (``lazy``, ``lazy_with_rebias``, ``jit``) it returns
        ``None`` — those patterns load memories after the first user turn.

        Returns ``None`` when the backend produces no results (including
        ``NullMemoryClient``), when no project config is available, or
        when the loading pattern defers retrieval.

        Subclasses override this to customise the query, formatting, or
        to return ``None`` unconditionally if they prefer per-turn recall.
        """
        # Resolve loading pattern: config-level takes precedence, then SDK,
        # then default to eager.
        config_pattern = self.config.memory.loading_pattern
        project_config = self.memory.project_config

        if config_pattern is not None:
            pattern = config_pattern
        elif project_config is not None:
            try:
                pattern = project_config.memory_loading.pattern
            except AttributeError:
                pattern = "eager"  # pre-pattern SDK — treat as eager
        else:
            pattern = "eager"

        if pattern != "eager":
            logger.debug(
                "Memory loading pattern is %r — deferring to post-turn retrieval",
                pattern,
            )
            return None

        # Eager path: retrieve memories at setup time.
        if project_config is not None:
            search_kwargs: dict[str, Any] = {"mode": "index", "max_results": self.config.memory.max_results}
            project_id = getattr(project_config, "project_id", None)
            if project_id:
                search_kwargs["project_id"] = project_id
            results = await self.memory.search("", **search_kwargs)
        else:
            # No project config (non-MemoryHub backend or old SDK) —
            # empty query returns all results in backend-native order,
            # which is the right behavior for markdown/sqlite backends.
            results = await self.memory.search("")

        if not results:
            return None

        min_w = self.config.memory.min_weight
        if min_w > 0:
            results = [r for r in results if r.get("weight", 1.0) >= min_w]

        parts = [r.get("content", "") for r in results]
        parts = [p for p in parts if p.strip()]  # drop blanks
        if not parts:
            return None

        joined = "\n\n---\n\n".join(parts)

        limit = self.config.memory.max_prefix_chars
        if limit and len(joined) > limit:
            joined = joined[:limit] + "\n\n… [truncated]"

        return joined

    async def _inject_deferred_memory(self) -> None:
        """Inject memories after the first user message for deferred patterns.

        Called at the top of :meth:`astep_stream` before the first model call.
        No-ops for ``eager`` patterns (already handled by :meth:`setup`) and
        for non-MemoryHub backends.  Wraps all logic in try/except so memory
        failures never crash the agent loop.
        """
        try:
            # Resolve loading pattern: config-level > SDK > default (eager).
            config_pattern = self.config.memory.loading_pattern
            project_config = self.memory.project_config

            if config_pattern is not None:
                pattern = config_pattern
            elif project_config is not None:
                try:
                    pattern = project_config.memory_loading.pattern
                except AttributeError:
                    pattern = "eager"  # Pre-pattern SDK — eager already handled by setup().
            else:
                pattern = "eager"

            if not pattern or pattern == "eager":
                return  # Already handled in setup().

            # Find the last user message to use as the search query.
            user_msg: dict[str, Any] | None = None
            user_msg_idx: int = -1
            for i in range(len(self.messages) - 1, -1, -1):
                if self.messages[i].get("role") == "user":
                    user_msg = self.messages[i]
                    user_msg_idx = i
                    break

            if user_msg is None:
                return

            # ``content`` may be a plain string or, for multimodal turns,
            # a list of OpenAI-shaped content blocks. Build the search
            # query by joining text from text-typed blocks; image blocks
            # contribute nothing to retrieval.
            original_content = user_msg.get("content", "") or ""
            if isinstance(original_content, list):
                query = "\n".join(
                    b.get("text", "")
                    for b in original_content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                query = original_content
            tag = self.config.memory.injection_tag
            injection_mode = self.config.memory.injection_mode

            # Guard against double injection in user_turn mode.
            if injection_mode == "user_turn" and f"<{tag}>" in query:
                return

            # Search memory using the user message as the query.
            search_kwargs: dict[str, Any] = {"max_results": self.config.memory.max_results}
            project_id = getattr(project_config, "project_id", None)
            if project_id:
                search_kwargs["project_id"] = project_id
            results = await self.memory.search(query, **search_kwargs)

            if not results:
                return

            min_w = self.config.memory.min_weight
            if min_w > 0:
                results = [r for r in results if r.get("weight", 1.0) >= min_w]
            if not results:
                return

            parts = [r.get("content", "") for r in results]
            parts = [p for p in parts if p.strip()]
            if not parts:
                return

            joined = "\n\n---\n\n".join(parts)

            limit = self.config.memory.max_prefix_chars
            if limit and len(joined) > limit:
                joined = joined[:limit] + "\n\n… [truncated]"

            if injection_mode == "user_turn":
                if isinstance(original_content, list):
                    # Append a new trailing text block so image-only
                    # messages survive (concatenating onto an empty
                    # string would drop the image references).
                    self.messages[user_msg_idx]["content"] = list(original_content) + [
                        {"type": "text", "text": f"<{tag}>\n{joined}\n</{tag}>"}
                    ]
                else:
                    self.messages[user_msg_idx]["content"] = (
                        original_content + f"\n\n<{tag}>\n{joined}\n</{tag}>"
                    )
                logger.debug(
                    "Deferred memory injected into user turn (%d chars, pattern=%r)",
                    len(joined), pattern,
                )
            else:
                # prefix mode: insert a new message immediately before the user message.
                msg = {"role": self.config.memory.prefix_role, "content": joined}
                _stamp_message_id(msg)
                self.messages.insert(user_msg_idx, msg)
                logger.debug(
                    "Deferred memory injected as prefix before user turn "
                    "(%d chars, role=%r, pattern=%r)",
                    len(joined), self.config.memory.prefix_role, pattern,
                )
        except Exception:
            logger.warning(
                "Deferred memory injection failed — continuing without memories",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# MCP tool registration helper
# ---------------------------------------------------------------------------


def _register_mcp_tool(
    registry: ToolRegistry,
    client_ref: _McpClientRef,
    mcp_tool: Any,
    *,
    reconnect_fn: Any | None = None,
) -> None:
    """Wrap an MCP tool as a local callable and register it (llm_only).

    The closure captures *client_ref* (not the raw client), so that an
    auth-refresh reconnect transparently updates all tool closures.
    """
    from fipsagents.baseagent.tools import ToolMeta, _TOOL_MARKER

    tool_name = mcp_tool.name
    tool_desc = getattr(mcp_tool, "description", "") or tool_name
    input_schema = getattr(mcp_tool, "inputSchema", None) or {}

    async def _call_mcp_tool(**kwargs: Any) -> str:
        try:
            result = await client_ref.client.call_tool(tool_name, kwargs)
        except Exception as exc:
            if reconnect_fn is not None and _is_auth_error(exc):
                logger.info(
                    "Auth error on MCP tool %r (server=%s) — attempting refresh",
                    tool_name, client_ref.label,
                )
                refreshed = await reconnect_fn(client_ref)
                if refreshed:
                    result = await client_ref.client.call_tool(tool_name, kwargs)
                else:
                    raise
            else:
                raise
        # Extract text from MCP CallToolResult content items.
        # Each item is typically TextContent with a .text attribute.
        parts = []
        for item in getattr(result, "content", []):
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
        return "\n".join(parts) if parts else str(result)

    meta = ToolMeta(
        name=tool_name,
        description=tool_desc,
        visibility="llm_only",
        fn=_call_mcp_tool,
        is_async=True,
        parameters=input_schema,
    )
    setattr(_call_mcp_tool, _TOOL_MARKER, meta)

    try:
        registry.register(_call_mcp_tool)
    except ValueError:
        logger.warning(
            "MCP tool %r conflicts with an existing tool name — skipping",
            tool_name,
        )


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------


def _is_auth_error(exc: Exception) -> bool:
    """Detect authentication/authorization errors from MCP transports."""
    if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        return exc.response.status_code in (401, 403)
    if type(exc).__name__ == "AuthorizationError":
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in ("401", "unauthorized", "403", "forbidden"))


def _doom_loop_hash(tool_name: str, args: dict, mode: str = "structured") -> str:
    """Compute a canonical hash of a tool call for doom-loop detection."""
    import hashlib
    import json as _json
    if mode == "structured":
        canonical = _json.dumps({"tool": tool_name, "args": args}, sort_keys=True, default=str)
    else:
        canonical = f"{tool_name}:{args}"
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _summarise_kwargs(kwargs: dict[str, Any], max_len: int = 120) -> str:
    """Produce a compact string summary of kwargs for log messages."""
    if not kwargs:
        return ""
    parts = [f"{k}={_truncate(repr(v), 40)}" for k, v in kwargs.items()]
    joined = ", ".join(parts)
    return _truncate(joined, max_len)


def _truncate(text: str, max_len: int = 200) -> str:
    """Truncate a string and append '...' if it exceeds *max_len*."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
