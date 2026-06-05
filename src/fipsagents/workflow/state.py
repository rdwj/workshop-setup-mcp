"""WorkflowState base class and END sentinel.

State carries ONLY data. No execution metadata, no step counters, no
node references. This is a hard constraint that keeps state serialisable
and diffable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# Sentinel returned by edge functions to signal workflow termination.
END: str = "__END__"


class WorkflowState(BaseModel):
    """Base class for all workflow state objects.

    Uses ``extra="forbid"`` so typos in field names raise immediately
    rather than silently creating unexpected attributes.

    Subclass this with your workflow's typed fields::

        class MyState(WorkflowState):
            query: str = ""
            results: list[str] = []
            error: str | None = None
    """

    model_config = ConfigDict(extra="forbid")
