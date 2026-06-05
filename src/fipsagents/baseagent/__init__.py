"""BaseAgent framework for building production-ready AI agents."""

__version__ = "0.9.0"

from fipsagents.baseagent.agent import BaseAgent, StepOutcome, StepResult
from fipsagents.baseagent.config import AgentConfig, AgentIdentity, ConfigError, HookEntryConfig, NodeConfig, PromptAssemblyConfig, SecurityConfig, ServerConfig, StateRecoveryConfig, load_config, load_config_from_string
from fipsagents.baseagent.hooks import HookEntry, HookResult, HookRunner
from fipsagents.baseagent.events import (
    ContentDelta,
    GuardrailFiredEvent,
    ReasoningDelta,
    StreamComplete,
    StreamEvent,
    StreamMetrics,
    ToolCallDelta,
    ToolResultEvent,
)
from fipsagents.baseagent.llm import (
    LLMClient,
    LLMError,
    ModelResponse,
    ModerationResult,
    PlatformResponse,
)
from fipsagents.baseagent.memory import MemoryClientBase, NullMemoryClient, create_memory_client
from fipsagents.baseagent.prompts import Prompt, PromptLoader
from fipsagents.baseagent.rules import Rule, RuleLoader
from fipsagents.baseagent.skills import Skill, SkillLoader
from fipsagents.baseagent.prompt_assembly import PromptAssembler, PromptAssemblyAudit, PromptLayer
from fipsagents.baseagent.diagnostics import RoleProbeResult, probe_role_support
from fipsagents.baseagent.tool_inspector import InspectionFinding, InspectionResult, ToolInspector
from fipsagents.baseagent.tools.question import QuestionAnswer, QuestionOption
from fipsagents.baseagent.state import AgentState
from fipsagents.baseagent.tools import ToolCall, ToolRegistry, ToolResult, tool

__all__ = [
    # agent
    "BaseAgent",
    "StepOutcome",
    "StepResult",
    # config
    "AgentConfig",
    "AgentIdentity",
    "ConfigError",
    "HookEntryConfig",
    "NodeConfig",
    "PromptAssemblyConfig",
    "SecurityConfig",
    "ServerConfig",
    "StateRecoveryConfig",
    "load_config",
    "load_config_from_string",
    # hooks
    "HookEntry",
    "HookResult",
    "HookRunner",
    # events (streaming)
    "ContentDelta",
    "GuardrailFiredEvent",
    "ReasoningDelta",
    "StreamComplete",
    "StreamEvent",
    "StreamMetrics",
    "ToolCallDelta",
    "ToolResultEvent",
    # llm
    "LLMClient",
    "LLMError",
    "ModelResponse",
    "ModerationResult",
    "PlatformResponse",
    # memory
    "MemoryClientBase",
    "NullMemoryClient",
    "create_memory_client",
    # prompts
    "Prompt",
    "PromptLoader",
    # rules
    "Rule",
    "RuleLoader",
    # skills
    "Skill",
    "SkillLoader",
    # prompt_assembly
    "PromptAssembler",
    "PromptAssemblyAudit",
    "PromptLayer",
    # diagnostics
    "RoleProbeResult",
    "probe_role_support",
    # tool_inspector
    "InspectionFinding",
    "InspectionResult",
    "ToolInspector",
    # question_tool
    "QuestionAnswer",
    "QuestionOption",
    # state
    "AgentState",
    # tools
    "tool",
    "ToolCall",
    "ToolRegistry",
    "ToolResult",
]
