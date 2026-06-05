"""Message compaction backends.

Compactors reduce the length of a conversation's message history by
summarising or pruning older turns.  The server invokes the compactor
before the model call when the message list exceeds a configured
threshold.  ``NullCompactor`` (default) is a no-op -- fully
backward-compatible.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CompactionState:
    """Serialisable state tracking compaction history for a session."""

    last_compacted_at: str | None = None
    last_compacted_message_id: str | None = None
    compaction_count: int = 0


@dataclass
class CompactionResult:
    """Result of a compaction attempt."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    original_count: int = 0
    compacted_count: int = 0
    skipped: bool = False
    skip_reason: str | None = None


class Compactor(ABC):
    """Pluggable message compaction backend."""

    @abstractmethod
    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        *,
        state: CompactionState | None = None,
    ) -> bool:
        """Return True if the message list should be compacted."""

    @abstractmethod
    async def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        state: CompactionState | None = None,
    ) -> CompactionResult:
        """Compact the message list. Must preserve ``id`` fields on
        surviving messages."""

    async def close(self) -> None:
        """Release resources. Default no-op."""


class NullCompactor(Compactor):
    """No compaction -- messages pass through unchanged."""

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        *,
        state: CompactionState | None = None,
    ) -> bool:
        return False

    async def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        state: CompactionState | None = None,
    ) -> CompactionResult:
        return CompactionResult(
            messages=messages,
            original_count=len(messages),
            compacted_count=len(messages),
            skipped=True,
            skip_reason="null_compactor",
        )


_SUMMARY_PROMPT = """\
Summarize the following conversation history into a concise briefing. \
Structure your summary with these sections:

**Goal**: The user's stated objective for this session.
**Active constraints**: Any "do not do X" or "always Y" directives still in force.
**Progress**: What has been done and what is in-flight.
**Decisions made**: Choices committed to by the agent and operator.
**Pending questions**: Open clarifications awaiting answers.
**Files/artifacts**: File IDs, document URIs, or prompt names referenced.
**Last user intent**: The most recent user turn's intent in one sentence.

Be concise. Preserve specific names, IDs, and values. \
Do not add information that is not in the conversation."""


class LLMCompactor(Compactor):
    """Summarise older messages using an LLM call."""

    def __init__(
        self,
        model_fn: Any,
        *,
        threshold_messages: int = 50,
        keep_recent_turns: int = 4,
        summary_role: str = "developer",
        context_limit: int = 0,
        reserve_tokens: int = 4000,
    ) -> None:
        self._model_fn = model_fn
        self._threshold = threshold_messages
        self._keep_recent = keep_recent_turns
        self._summary_role = summary_role
        self._context_limit = context_limit
        self._reserve_tokens = reserve_tokens

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        return len(str(messages)) // 4

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        *,
        state: CompactionState | None = None,
    ) -> bool:
        non_system = [m for m in messages if m.get("role") != "system"]
        if len(non_system) >= self._threshold:
            return True
        if self._context_limit > 0:
            estimated = self._estimate_tokens(messages)
            if estimated > self._context_limit - self._reserve_tokens:
                return True
        return False

    async def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        state: CompactionState | None = None,
    ) -> CompactionResult:
        original_count = len(messages)

        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if not non_system:
            return CompactionResult(
                messages=messages,
                original_count=original_count,
                compacted_count=original_count,
                skipped=True,
                skip_reason="no_non_system_messages",
            )

        # Walk backward to find the boundary preserving keep_recent_turns
        # user/assistant exchange pairs.
        preserve_idx = len(non_system)
        pairs_found = 0
        i = len(non_system) - 1
        while i >= 0 and pairs_found < self._keep_recent:
            msg = non_system[i]
            if msg.get("role") == "user":
                pairs_found += 1
                preserve_idx = i
            i -= 1

        preserved = non_system[preserve_idx:]
        compactable = non_system[:preserve_idx]

        # Tool-call pairing guard: ensure tool_calls and their results
        # are never split across the compactable/preserved boundary.
        preserved_call_ids: set[str] = set()
        for m in preserved:
            for tc in m.get("tool_calls", []):
                tc_id = tc.get("id", "")
                if tc_id:
                    preserved_call_ids.add(tc_id)

        # Move tool results from compactable whose call is in preserved.
        moved_to_preserved: list[int] = []
        for idx, m in enumerate(compactable):
            if m.get("role") == "tool" and m.get("tool_call_id") in preserved_call_ids:
                moved_to_preserved.append(idx)

        # Move tool_calls assistants from compactable whose results are
        # in preserved.
        preserved_result_ids: set[str] = set()
        for m in preserved:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                preserved_result_ids.add(m["tool_call_id"])
        for idx, m in enumerate(compactable):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    if tc.get("id") in preserved_result_ids:
                        if idx not in moved_to_preserved:
                            moved_to_preserved.append(idx)
                        break

        if moved_to_preserved:
            # Extend the boundary backward to include all moved messages.
            # The simplest correct approach: find the lowest index that
            # needs to move, then move *everything* from there onward
            # into preserved.
            new_boundary = min(moved_to_preserved)
            preserved = non_system[new_boundary:]
            compactable = non_system[:new_boundary]

        # Pending-state guard: skip if any compactable message contains
        # pending sentinels.
        for m in compactable:
            content = m.get("content", "")
            if isinstance(content, str) and (
                "__pending__" in content or "__permission_pending__" in content
            ):
                return CompactionResult(
                    messages=messages,
                    original_count=original_count,
                    compacted_count=original_count,
                    skipped=True,
                    skip_reason="pending_state",
                )

        if not compactable:
            return CompactionResult(
                messages=messages,
                original_count=original_count,
                compacted_count=original_count,
                skipped=True,
                skip_reason="nothing_to_compact",
            )

        # Build the summary prompt and call the LLM.
        serialized = json.dumps(compactable, indent=2, default=str)
        summary_messages = [
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user", "content": serialized},
        ]

        try:
            summary_text = await self._model_fn(summary_messages)
        except Exception:
            logger.exception("LLM compaction failed; returning original messages")
            return CompactionResult(
                messages=messages,
                original_count=original_count,
                compacted_count=original_count,
                skipped=True,
                skip_reason="llm_error",
            )

        summary_msg: dict[str, Any] = {
            "role": self._summary_role,
            "content": summary_text,
        }

        compacted = system_msgs + [summary_msg] + preserved
        return CompactionResult(
            messages=compacted,
            original_count=original_count,
            compacted_count=len(compacted),
        )


def create_compactor(
    backend: str | None = None,
    *,
    model_fn: Any | None = None,
    **kwargs: Any,
) -> Compactor:
    """Create a compactor from config."""
    if backend is None or backend == "null":
        return NullCompactor()
    if backend == "llm":
        if model_fn is None:
            raise ValueError("LLMCompactor requires a model_fn callable")
        return LLMCompactor(model_fn, **kwargs)
    raise ValueError(f"Unknown compactor backend: {backend!r}")
