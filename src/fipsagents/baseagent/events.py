"""Typed events emitted by ``BaseAgent.astep_stream``.

A streaming agent run produces a sequence of these events. Server code
serializes them to whatever wire format the consumer expects:

- The standard ``/v1/chat/completions`` SSE shape uses only standard
  OpenAI delta fields (``reasoning_content``, ``tool_calls``,
  ``role="tool"`` + ``tool_call_id``, ``content``). No custom fields
  required.
- A future ``/v1/responses`` endpoint can serialize the same event
  stream to the OpenAI Responses API event protocol used by LlamaStack.

Events are intentionally framework-internal. Consumers depend on this
typed surface, not on litellm chunk shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass
class ReasoningDelta:
    """Incremental chunk of model reasoning ("thinking")."""

    content: str


@dataclass
class ToolCallDelta:
    """Incremental chunk of a tool-call decision streamed from the model.

    The first delta for a given ``index`` carries ``call_id`` and
    ``name``. Subsequent deltas for the same ``index`` only carry
    ``arguments_delta`` — the JSON arguments string streamed
    token-by-token. Consumers should accumulate ``arguments_delta`` per
    ``index`` until the model finishes the tool-call decision.
    """

    index: int
    call_id: str | None = None
    name: str | None = None
    arguments_delta: str = ""


@dataclass
class ToolResultEvent:
    """Result of executing a tool the model decided to call.

    Emitted after the agent runs the tool. ``call_id`` matches the
    ``call_id`` from the originating ``ToolCallDelta`` so consumers can
    pair decisions with results.
    """

    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class ContentDelta:
    """Incremental chunk of the user-visible assistant response."""

    content: str


@dataclass
class GuardrailFiredEvent:
    """A platform-side shield (Llama Guard, Prompt Guard, etc.) fired on
    the current turn.

    Emitted when ``platform.enabled`` is True and OGX reports a shield
    activation in the Responses event stream. ``action`` distinguishes
    advisory firings (``warned``) from terminal ones (``blocked``); a
    ``blocked`` event will be followed by ``StreamComplete`` with
    ``finish_reason="guardrail"``.

    ``shield_id`` matches an entry in ``platform.guardrails``.
    ``category`` is the classifier label OGX returned (e.g.
    ``"hate"``, ``"jailbreak"``); ``None`` when the shield does not
    expose categories. ``message`` is the optional human-readable
    explanation from the shield.
    """

    shield_id: str
    action: str  # "blocked" | "warned"
    category: str | None = None
    message: str | None = None


@dataclass
class StreamMetrics:
    """Per-stream timing and token counts.

    Captured incrementally during the stream and finalized in
    ``StreamComplete``. Times are seconds since the stream began.
    Counts come from the provider's usage block when available;
    otherwise they remain ``None``.
    """

    time_to_first_reasoning: float | None = None
    time_to_first_content: float | None = None
    total_time: float = 0.0
    inter_token_latencies: list[float] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    model_calls: int = 0
    tool_calls: int = 0


@dataclass
class StreamComplete:
    """Terminal event for a streaming agent run."""

    finish_reason: str
    metrics: StreamMetrics


@dataclass
class SubagentInvoked:
    """Emitted when a delegate_to_agent tool call begins.

    The parent's stream sees this immediately after the LLM's tool-call
    decision, before the subagent runs. Use ``span_id`` to correlate with
    the matching ``SubagentCompleted`` / ``SubagentFailed`` event.
    """

    agent_name: str
    task: str
    span_id: str
    transport: str  # "remote" | "inprocess"
    depth: int


@dataclass
class SubagentCompleted:
    """Emitted when a subagent invocation finishes successfully.

    Carries the same payload that the parent's tool result will surface
    to the LLM, plus the rolled-up token and cost telemetry.
    """

    agent_name: str
    span_id: str
    content: str
    tokens_used: dict[str, int]
    tool_calls_made: int
    cost_usd: float


@dataclass
class SubagentFailed:
    """Emitted when a subagent invocation fails (timeout, remote error,
    max depth, crash, etc.).
    """

    agent_name: str
    span_id: str
    error_type: str  # short type name, e.g. "Timeout", "RemoteError"
    error_message: str


@dataclass
class SubagentDelta:
    """Forward-compat variant for v2 streaming.

    v1 ships buffered subagent calls and never emits this event; v2 will
    forward the subagent's intermediate stream onto the parent's stream
    so the gateway can render nested deltas. Defined now so consumers can
    pattern-match exhaustively.
    """

    agent_name: str
    span_id: str
    delta: object  # the original StreamEvent from the subagent


@dataclass
class CompactionStarted:
    """Emitted when message compaction begins."""
    session_id: str | None = None
    message_count: int = 0


@dataclass
class CompactionCompleted:
    """Emitted when message compaction finishes successfully."""
    session_id: str | None = None
    original_count: int = 0
    compacted_count: int = 0


@dataclass
class CompactionSkipped:
    """Emitted when compaction is skipped."""
    reason: str
    session_id: str | None = None


@dataclass
class PermissionDecisionMade:
    """Emitted when a permission check resolves for a tool call."""
    tool: str
    action: str  # "allow" | "deny" | "ask"
    rule_id: str | None = None
    scope: str | None = None


@dataclass
class QuestionAsked:
    """Emitted when the agent poses a structured question to the operator."""
    question_id: str
    question_text: str
    options: list[dict[str, Any]] = field(default_factory=list)
    multiple: bool = False
    allow_custom: bool = False
    session_id: str | None = None


@dataclass
class QuestionAnswered:
    """Emitted when the operator answers a pending question."""
    question_id: str
    answer_text: str
    session_id: str | None = None


@dataclass
class LimitExceeded:
    """Emitted when a per-turn resource limit is breached."""
    limit_type: str  # "tokens" | "iterations" | "cost"
    threshold: float
    actual: float


@dataclass
class LoopBreakEvent:
    """Emitted when the doom-loop guard detects repeated tool calls."""
    tool_name: str
    repeat_count: int
    last_args: dict[str, Any]
    last_error: str | None = None


@dataclass
class StateCheckpointed:
    """Emitted when agent state is checkpointed to persistent storage."""
    session_id: str
    checkpoint_at: str
    state_type: str


@dataclass
class StateRecovered:
    """Emitted when agent state is recovered from checkpoint + replay."""
    session_id: str
    checkpoint_at: str
    events_replayed: int
    state_type: str


@dataclass
class EventReceived:
    """Emitted when an inbound event arrives from a source."""
    event_id: str
    event_type: str
    source: str


@dataclass
class EventProcessed:
    """Emitted when event processing completes successfully."""
    event_id: str
    source: str
    duration_ms: float


@dataclass
class EventFailed:
    """Emitted when event processing fails."""
    event_id: str
    source: str
    error: str
    retriable: bool


@dataclass
class WorkItemCheckedOut:
    """Emitted when an agent checks out a work item."""
    item_id: str
    actor_id: str
    title: str


@dataclass
class WorkItemCompleted:
    """Emitted when an agent completes a work item."""
    item_id: str
    actor_id: str
    title: str


@dataclass
class WorkItemReleased:
    """Emitted when an agent releases a work item back to the pool."""
    item_id: str
    actor_id: str
    title: str


@dataclass
class WorkItemFailed:
    """Emitted when an agent fails a work item."""
    item_id: str
    actor_id: str
    title: str
    error: str


@dataclass
class BudgetHeadroomWarning:
    """Emitted when budget is approaching the headroom threshold."""
    item_id: str
    remaining_pct: float


@dataclass
class HandoffRequired:
    """Emitted when a handoff is needed before lease expiry."""
    item_id: str
    actor_id: str
    expires_at: str


@dataclass
class TrustLevelChanged:
    """Emitted when an agent's trust level changes (promotion or demotion)."""
    from_level: int
    to_level: int
    score: float
    reason: str


@dataclass
class SkillLearned:
    """Emitted when an agent creates or updates a learned skill."""
    skill_name: str
    domain: str
    version: int
    review_status: str


@dataclass
class SkillProposed:
    """Emitted when an agent proposes a skill for review via suggest_skill."""
    skill_name: str
    description: str
    content: str
    domain: str
    trigger: str
    work_item_id: str | None = None


@dataclass
class SkillEdited:
    """Emitted when a learned skill is edited (version bump)."""
    skill_name: str
    from_version: int
    to_version: int


@dataclass
class SkillRolledBack:
    """Emitted when a learned skill is rolled back to a prior version."""
    skill_name: str
    from_version: int
    to_version: int
    reason: str


@dataclass
class SkillQuarantined:
    """Emitted when a learned skill is quarantined due to trust violation."""
    skill_name: str
    reason: str


@dataclass
class StagePromoted:
    """Emitted when an agent advances to a higher maturation stage."""
    from_stage: str
    to_stage: str
    trust_level: int
    reason: str


@dataclass
class StageDemoted:
    """Emitted when an agent drops to a lower maturation stage."""
    from_stage: str
    to_stage: str
    trust_level: int
    reason: str


@dataclass
class SpawnAgentInvoked:
    """Emitted when a spawn_agent tool call begins."""
    role: str
    task: str
    span_id: str
    tools: list[str]
    model: str | None
    depth: int


@dataclass
class SpawnAgentCompleted:
    """Emitted when a spawned ephemeral agent finishes successfully."""
    role: str
    span_id: str
    content: str
    tokens_used: dict[str, int]
    tool_calls_made: int
    cost_usd: float


@dataclass
class SpawnAgentFailed:
    """Emitted when a spawned ephemeral agent fails."""
    role: str
    span_id: str
    error_type: str
    error_message: str


# Discriminated union of every event a stream can emit.
StreamEvent = Union[
    ReasoningDelta,
    ToolCallDelta,
    ToolResultEvent,
    ContentDelta,
    GuardrailFiredEvent,
    StreamComplete,
    SubagentInvoked,
    SubagentCompleted,
    SubagentFailed,
    SubagentDelta,
    CompactionStarted,
    CompactionCompleted,
    CompactionSkipped,
    PermissionDecisionMade,
    QuestionAsked,
    QuestionAnswered,
    LimitExceeded,
    LoopBreakEvent,
    StateCheckpointed,
    StateRecovered,
    EventReceived,
    EventProcessed,
    EventFailed,
    WorkItemCheckedOut,
    WorkItemCompleted,
    WorkItemReleased,
    WorkItemFailed,
    BudgetHeadroomWarning,
    HandoffRequired,
    TrustLevelChanged,
    SkillLearned,
    SkillProposed,
    SkillEdited,
    SkillRolledBack,
    SkillQuarantined,
    StagePromoted,
    StageDemoted,
    SpawnAgentInvoked,
    SpawnAgentCompleted,
    SpawnAgentFailed,
]
