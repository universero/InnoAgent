"""Tool registry and guarded execution pipeline."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from core.guardrails.base import BaseGuardrail, GuardrailDecision
from core.guardrails.file_guard import (
    FileConfirmationGuard,
    PathGuard,
    ReadOnlyGuard,
    WriteBeforeReadGuard,
)
from core.guardrails.plan_guard import PlanGuard
from core.tool.base import (
    BaseTool,
    ToolAuthorization,
    ToolContext,
    ToolResult,
    ToolSpec,
)


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
        return [spec.model_dump() for spec in self.tool_specs(context)]

    def tool_specs(self, context: ToolContext | None = None) -> list[ToolSpec]:
        """Return typed tool contracts used by model, policy, and UI adapters."""
        return [tool.spec(context) for tool in self.list_tools()]

    def normalize_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate and return the canonical arguments bound to authorization."""
        tool = self.get(name)
        return tool.validate(arguments).model_dump(mode="python")

    def authorize_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolAuthorization:
        """Evaluate pre-execution policy without invoking the tool."""
        try:
            tool = self.get(name)
        except KeyError as exc:
            return ToolAuthorization(
                tool_name=name,
                status="error",
                arguments=dict(arguments),
                reason=str(exc),
            )

        try:
            validated_arguments = tool.validate(arguments).model_dump(mode="python")
        except Exception as exc:  # noqa: BLE001 - validation is a policy boundary
            return ToolAuthorization(
                tool_name=name,
                status="error",
                arguments=dict(arguments),
                reason=f"invalid arguments: {exc}",
            )

        before_metadata: dict[str, Any] = {}
        for guardrail in self._guardrails:
            try:
                decision = guardrail.before(tool, validated_arguments, context)
            except Exception as exc:  # noqa: BLE001 - policy failure must fail closed
                return ToolAuthorization(
                    tool_name=name,
                    status="error",
                    arguments=validated_arguments,
                    reason=f"guardrail {guardrail.name} failed: {exc}",
                    metadata={"guardrail": guardrail.name},
                )
            detail = decision.metadata.get("details", decision.metadata)
            before_metadata[guardrail.name] = detail or decision.reason or "allowed"
            if decision.status == "needs_confirmation":
                return ToolAuthorization(
                    tool_name=name,
                    status="needs_confirmation",
                    arguments=validated_arguments,
                    reason=decision.reason,
                    metadata={
                        "guardrail": guardrail.name,
                        "arguments": validated_arguments,
                        "guardrails_before": before_metadata,
                        **decision.metadata,
                    },
                )
            if not decision.allowed or decision.status == "blocked":
                return ToolAuthorization(
                    tool_name=name,
                    status="blocked",
                    arguments=validated_arguments,
                    reason=decision.reason,
                    metadata={
                        "guardrail": guardrail.name,
                        "guardrails_before": before_metadata,
                        **decision.metadata,
                    },
                )
            resolved_path = decision.metadata.get("resolved_path")
            if resolved_path and "path" in validated_arguments:
                # 授权绑定规范化后的真实路径，执行阶段不再消费可被替换的原始符号链接。
                validated_arguments["path"] = str(resolved_path)
        return ToolAuthorization(
            tool_name=name,
            status="allowed",
            arguments=validated_arguments,
            metadata={"guardrails_before": before_metadata},
        )

    def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Run a tool after applying all registered guardrails."""
        authorization = self.authorize_tool(name, arguments, context)
        return self.execute_authorized(name, arguments, context, authorization)

    def execute_authorized(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        authorization: ToolAuthorization,
    ) -> ToolResult:
        """Execute a call after an explicit authorization decision."""
        if authorization.tool_name != name:
            return ToolResult(
                tool_name=name,
                status="error",
                output="authorization does not match requested tool",
            )
        if not authorization.allowed:
            return authorization.to_result()
        try:
            tool = self.get(name)
        except KeyError as exc:
            return ToolResult(tool_name=name, status="error", output=str(exc))
        # 只执行授权阶段校验并绑定的参数，避免调用方在授权后替换参数。
        result = tool.execute(authorization.arguments, context)
        result.metadata.update(authorization.metadata)

        for guardrail in reversed(self._guardrails):
            try:
                result = guardrail.after(tool, result, context)
            except Exception as exc:  # noqa: BLE001 - postflight errors remain observable
                return ToolResult(
                    tool_name=name,
                    status="error",
                    output=(
                        f"tool executed but guardrail {guardrail.name} postflight failed: {exc}; "
                        "side effects may have occurred"
                    ),
                    data=result.data,
                    warnings=[*result.warnings, "tool effect is uncertain"],
                    metadata={
                        **result.metadata,
                        **authorization.metadata,
                        "guardrail": guardrail.name,
                        "effect_uncertain": True,
                    },
                )
        max_chars = int(context.services.get("max_tool_output_chars", 30000))
        if len(result.output) > max_chars:
            omitted = len(result.output) - max_chars
            result.output = result.output[:max_chars] + f"\n[... {omitted} characters truncated]"
            if "tool output was truncated" not in result.warnings:
                result.warnings.append("tool output was truncated")
        return result

    def execute_many(
        self,
        calls: list[dict[str, Any]],
        context_factory: Callable[[], ToolContext],
        *,
        max_workers: int = 4,
        authorizations: list[ToolAuthorization] | None = None,
    ) -> list[ToolResult]:
        """Execute parallel-safe calls concurrently while preserving call order."""
        if authorizations is not None and len(authorizations) != len(calls):
            raise ValueError("authorizations must match calls")
        results: list[ToolResult | None] = [None] * len(calls)
        parallel_group: list[tuple[int, dict[str, Any]]] = []

        def flush_parallel() -> None:
            if not parallel_group:
                return
            with ThreadPoolExecutor(
                max_workers=max(1, min(max_workers, len(parallel_group)))
            ) as pool:
                futures = {
                    pool.submit(
                        self._execute_call,
                        str(call["name"]),
                        dict(call.get("arguments") or {}),
                        context_factory(),
                        authorizations[index] if authorizations is not None else None,
                    ): index
                    for index, call in parallel_group
                }
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
            parallel_group.clear()

        for index, call in enumerate(calls):
            tool = self._tools.get(str(call.get("name") or ""))
            if tool is not None and tool.parallel_safe and not tool.is_write:
                parallel_group.append((index, call))
                continue
            # 写工具和非并行工具是顺序屏障，前后的只读调用不能跨越它重排。
            flush_parallel()
            results[index] = self._execute_call(
                str(call.get("name") or ""),
                dict(call.get("arguments") or {}),
                context_factory(),
                authorizations[index] if authorizations is not None else None,
            )
        flush_parallel()
        return [result for result in results if result is not None]

    def _execute_call(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        authorization: ToolAuthorization | None,
    ) -> ToolResult:
        if authorization is None:
            return self.execute_tool(name, arguments, context)
        return self.execute_authorized(name, arguments, context, authorization)


# Runtime 与 CLI 共享同一注册表，内置工具通过模块导入完成注册。
tool_registry = ToolRegistry()
tool_registry.set_default_guardrails()
