"""Tool registry and guarded execution pipeline."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
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
        try:
            tool = self.get(name)
        except KeyError as exc:
            return ToolResult(
                tool_name=name,
                status="error",
                output=str(exc),
            )

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
        max_chars = int(context.services.get("max_tool_output_chars", 30000))
        if len(result.output) > max_chars:
            omitted = len(result.output) - max_chars
            result.output = result.output[:max_chars] + f"\n[... {omitted} characters truncated]"
            result.warnings.append("tool output was truncated")
        return result

    def execute_many(
        self,
        calls: list[dict[str, Any]],
        context_factory,
        *,
        max_workers: int = 4,
    ) -> list[ToolResult]:
        """Execute parallel-safe calls concurrently while preserving call order."""
        results: list[ToolResult | None] = [None] * len(calls)
        parallel: list[tuple[int, dict[str, Any]]] = []
        serial: list[tuple[int, dict[str, Any]]] = []
        for index, call in enumerate(calls):
            tool = self._tools.get(str(call.get("name") or ""))
            if tool is None:
                serial.append((index, call))
                continue
            target = parallel if tool.parallel_safe and not tool.is_write else serial
            target.append((index, call))

        if parallel:
            # 并发完成顺序不稳定，因此按原始索引回填，保持模型观察顺序确定。
            with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(parallel)))) as pool:
                futures = {
                    pool.submit(
                        self.execute_tool,
                        str(call["name"]),
                        dict(call.get("arguments") or {}),
                        context_factory(),
                    ): index
                    for index, call in parallel
                }
                for future in as_completed(futures):
                    results[futures[future]] = future.result()

        for index, call in serial:
            results[index] = self.execute_tool(
                str(call["name"]),
                dict(call.get("arguments") or {}),
                context_factory(),
            )
        return [result for result in results if result is not None]


# Runtime 与 CLI 共享同一注册表，内置工具通过模块导入完成注册。
tool_registry = ToolRegistry()
tool_registry.set_default_guardrails()
