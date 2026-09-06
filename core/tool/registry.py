"""Tool registry and guarded execution pipeline."""

from __future__ import annotations

from typing import Any

from core.guardrails.base import BaseGuardrail, GuardrailDecision
from core.guardrails.file_guard import (
    FileConfirmationGuard,
    PathGuard,
    ReadOnlyGuard,
    WriteBeforeReadGuard,
)
from core.guardrails.plan_guard import PlanGuard
from core.tool.base import BaseTool, ToolContext, ToolResult


class ToolRegistry:
    """Store tool classes and execute them through the guardrail chain."""

    def __init__(self) -> None:
        """Initialize empty tool and guardrail registries."""
        self._tools: dict[str, BaseTool] = {}
        self._guardrails: list[BaseGuardrail] = []

    def register(self, tool: BaseTool) -> BaseTool:
        """Register one tool instance."""
        if not tool.name:
            raise ValueError("tool name must not be empty")
        self._tools[tool.name] = tool
        return tool

    def register_guardrail(self, guardrail: BaseGuardrail) -> None:
        """Append a guardrail to the execution chain."""
        self._guardrails.append(guardrail)

    def set_default_guardrails(self) -> None:
        """Install the default file and mode guardrails."""
        self._guardrails = [
            PlanGuard(),
            PathGuard(),
            WriteBeforeReadGuard(),
            FileConfirmationGuard(),
            ReadOnlyGuard(),
        ]

    def get(self, name: str) -> BaseTool:
        """Return a registered tool by name."""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def list_tools(self) -> list[BaseTool]:
        """Return all registered tool instances."""
        return list(self._tools.values())

    def tool_schemas(self, context: ToolContext | None = None) -> list[dict[str, Any]]:
        """Return model-facing tool descriptors."""
        schemas: list[dict[str, Any]] = []
        for tool in self.list_tools():
            schemas.append(
                {
                    "name": tool.name,
                    "description": tool.dynamic_description(context),
                    "parameters": tool.input_model.model_json_schema(),
                    "is_write": tool.is_write,
                    "requires_confirmation": tool.requires_confirmation,
                }
            )
        return schemas

    def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Run a tool after applying all registered guardrails."""
        tool = self.get(name)

        before_metadata: dict[str, Any] = {}
        for guardrail in self._guardrails:
            decision = guardrail.before(tool, arguments, context)
            before_metadata[guardrail.name] = decision.reason or "allowed"
            if decision.status == "needs_confirmation":
                return ToolResult(
                    tool_name=name,
                    status="needs_confirmation",
                    output=decision.reason,
                    metadata={
                        "guardrail": guardrail.name,
                        "arguments": arguments,
                        **decision.metadata,
                    },
                )
            if not decision.allowed or decision.status == "blocked":
                return ToolResult(
                    tool_name=name,
                    status="blocked",
                    output=decision.reason,
                    metadata={"guardrail": guardrail.name, **decision.metadata},
                )
            before_metadata[guardrail.name] = decision.metadata.get(
                "details", decision.metadata
            )

        result = tool.execute(arguments, context)
        result.metadata["guardrails_before"] = before_metadata

        for guardrail in reversed(self._guardrails):
            result = guardrail.after(tool, result, context)
        return result


# Shared registry used by the runtime and CLI.
tool_registry = ToolRegistry()
tool_registry.set_default_guardrails()
