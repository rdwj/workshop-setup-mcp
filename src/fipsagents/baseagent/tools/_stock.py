"""Declarative descriptors for stock (framework-provided) tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class StockToolSpec:
    """Descriptor for a stock tool that needs the agent instance.

    Attributes:
        factory: Callable that takes (agent) and returns a @tool-decorated function.
        condition: Optional callable that takes (agent) and returns bool.
                   When provided and returns False, the tool is not registered.
    """

    factory: Callable[[Any], Any]
    condition: Callable[[Any], bool] | None = None
