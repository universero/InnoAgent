"""Simple execution metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunMetrics:
    """Simple counters for runtime observability."""
    iterations: int = 0
    tool_calls: int = 0
    planning_calls: int = 0
    reflection_calls: int = 0
    guardrail_blocks: int = 0
    errors: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def record_event(self, event: str, data: dict[str, Any]) -> None:
        """Increment counters based on an emitted runtime event."""
        self.events.append({"event": event, "data": data})
        if event == "tool_use":
            self.tool_calls += 1
            if data.get("status") in {"blocked", "error"}:
                if data.get("status") == "blocked":
                    self.guardrail_blocks += 1
                else:
                    self.errors += 1
        elif event == "planning":
            self.planning_calls += 1
        elif event == "reflection":
            self.reflection_calls += 1

    def as_dict(self) -> dict[str, int]:
        """Return counters as a plain dictionary."""
        return {
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "planning_calls": self.planning_calls,
            "reflection_calls": self.reflection_calls,
            "guardrail_blocks": self.guardrail_blocks,
            "errors": self.errors,
        }
