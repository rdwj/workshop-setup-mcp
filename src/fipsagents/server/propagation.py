"""W3C Trace Context propagation utilities.

Provides ``extract_trace_context`` and ``inject_trace_context`` for
reading and writing ``traceparent`` / ``tracestate`` headers per
the W3C Trace Context specification.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping


def _string_to_trace_id(s: str) -> int:
    """Deterministic conversion of string ID to 128-bit trace ID."""
    return int.from_bytes(
        hashlib.sha256(s.encode()).digest()[:16], "big",
    )


def _string_to_span_id(s: str) -> int:
    """Deterministic conversion of string ID to 64-bit span ID."""
    return int.from_bytes(
        hashlib.sha256(s.encode()).digest()[:8], "big",
    )

# W3C traceparent format: version-trace_id-parent_id-trace_flags
# Example: 00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01
_TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)


@dataclass(frozen=True)
class TraceContext:
    """Parsed W3C Trace Context from incoming request headers."""

    trace_id: str  # 32 hex chars
    parent_span_id: str  # 16 hex chars
    trace_flags: str = "01"  # 2 hex chars


def extract_trace_context(
    headers: Mapping[str, str],
) -> TraceContext | None:
    """Extract W3C Trace Context from HTTP headers.

    Returns ``None`` if no ``traceparent`` header is present or
    if the header is malformed.
    """
    traceparent = headers.get("traceparent")
    if not traceparent:
        return None
    match = _TRACEPARENT_RE.match(traceparent.strip())
    if not match:
        return None
    _, trace_id, parent_span_id, trace_flags = match.groups()
    return TraceContext(
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        trace_flags=trace_flags,
    )


def inject_trace_context(
    trace_id: str,
    span_id: str,
) -> dict[str, str]:
    """Build W3C traceparent header for outgoing requests.

    Converts internal string IDs to hex format via deterministic
    hashing (same mapping used by ``OTELTraceStore``).
    """
    trace_id_hex = format(_string_to_trace_id(trace_id), "032x")
    span_id_hex = format(_string_to_span_id(span_id), "016x")
    return {
        "traceparent": f"00-{trace_id_hex}-{span_id_hex}-01",
    }
