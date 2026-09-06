"""Guardrail protocol and decision types."""

from __future__ import annotations

from abc import ABC
from typing import Any

from pydantic import BaseModel, Field

from core.tool.base import BaseTool, ToolContext, ToolResult


class GuardrailDecision(BaseModel):
    """Outcome produced by a guardrail before or after tool execution."""

    allowed: bool = True
    status: str | None = None
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseGuardrail(ABC):
    """A policy check applied around tool execution."""

    name: str

    def before(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> GuardrailDecision:
        """Run before tool execution and return allow/block/confirm."""
        return GuardrailDecision()

    def after(
        self,
        tool: BaseTool,
        result: ToolResult,
        context: ToolContext,
    ) -> ToolResult:
        """Transform or inspect a completed tool result."""
        return result
