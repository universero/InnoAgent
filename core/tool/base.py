"""Core tool abstractions.

The runtime communicates with tools through the small set of types defined
here.  Keeping this module dependency-free makes it easy to add tools without
creating circular imports: tool implementations may depend on this module, but
this module must not depend on tool implementations or the runtime graph.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """A validated request to execute one tool."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Uniform result returned by every tool execution."""

    tool_name: str
    status: Literal["success", "error", "needs_confirmation", "blocked"]
    output: str = ""
    data: Any = None
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return whether the tool completed successfully."""
        return self.status == "success"

    def to_message(self) -> str:
        """Render a compact message for the model."""
        status_label = {
            "success": "ok",
            "error": "error",
            "needs_confirmation": "needs user confirmation",
            "blocked": "blocked by guardrail",
        }[self.status]
        parts = [f"[{self.tool_name}] {status_label}"]
        if self.output:
            parts.append(self.output)
        if self.warnings:
            parts.append("warnings: " + "; ".join(self.warnings))
        return "\n".join(parts)


class EmptyInput(BaseModel):
    """Default input schema for tools without arguments."""


@dataclass
class ToolContext:
    """Execution context available to a tool and guardrails."""

    mode: Literal["auto", "confirm", "readonly"] = "auto"
    allowed_roots: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    approved_tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def resolve_path(self, path: str) -> Path:
        """Resolve relative paths against the first allowed workspace root."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() and self.allowed_roots:
            candidate = Path(self.allowed_roots[0]) / candidate
        return candidate.resolve()


class BaseTool(ABC):
    """Base class for built-in and extension tools."""

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]] = EmptyInput

    # Tool policy hints consumed by guardrails.
    is_write: ClassVar[bool] = False
    requires_confirmation: ClassVar[bool] = False

    def dynamic_description(self, context: ToolContext | None = None) -> str:
        """Return the description shown to the model for this invocation.

        Subclasses may override this to include live plan/task state.
        """
        return self.description

    def validate(self, arguments: dict[str, Any]) -> BaseModel:
        """Coerce and validate raw tool arguments."""
        return self.input_model.model_validate(arguments or {})

    @abstractmethod
    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        """Implement the actual tool behaviour."""

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Validate arguments and execute the tool."""
        try:
            parsed = self.validate(arguments)
        except Exception as exc:  # noqa: BLE001 - return a model-friendly error
            return ToolResult(
                tool_name=self.name,
                status="error",
                output=f"Invalid arguments: {exc}",
            )
        try:
            return self.run(parsed, context)
        except Exception as exc:  # noqa: BLE001 - tools must not crash the graph
            return ToolResult(
                tool_name=self.name,
                status="error",
                output=f"{type(exc).__name__}: {exc}",
            )


class ToolExecutionError(Exception):
    """Raised by tools when execution should be reported as an error."""
