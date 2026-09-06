"""Isolated read-only subagent execution."""

from __future__ import annotations

from typing import Any, Callable

from core.agent.model_stream import ModelStreamConsumer
from core.config.permissions import PermissionStore
from core.event.events import AgentEvent
from core.prompts import SUBAGENT_PROMPT
from core.runtime.config import RuntimeConfig
from core.tool.base import ToolContext, ToolResult
from core.tool.registry import ToolRegistry


class SubagentRunner:
    """Run a bounded child loop without sharing parent message history."""

    READ_ONLY_TOOLS = {"read", "ls", "grep"}

    def __init__(
        self,
        stream: ModelStreamConsumer,
        registry: ToolRegistry,
        config: RuntimeConfig,
        permissions: PermissionStore,
        emit: Callable[[AgentEvent], None],
    ) -> None:
        self.stream = stream
        self.registry = registry
        self.config = config
        self.permissions = permissions
        self.emit = emit

    def run(self, *, task: str, role: str, allowed_tools: list[str]) -> dict[str, Any]:
        # 子 Agent 的能力取调用方声明与只读白名单的交集，禁止权限继承扩张。
        allowed = [
            name
            for name in allowed_tools
            if name in self.READ_ONLY_TOOLS and name in self.registry._tools
        ]
        schemas = [schema for schema in self.registry.tool_schemas() if schema["name"] in allowed]
        local: dict[str, Any] = {
            # 不复制父会话历史，既控制 token，也隔离无关指令和敏感上下文。
            "user_input": task,
            "messages": [{"role": "user", "content": task}],
            "tool_results": [],
            "errors": [],
        }
        usage: dict[str, int] = {}
        context = f"{SUBAGENT_PROMPT}\n\nRole: {role}\nTask: {task}"
        for _ in range(6):
            batch = self.stream.call(context, local, stage="subagent", tool_schemas=schemas)
            self._add_usage(usage, batch.usage)
            if batch.error:
                return {"error": batch.error, "summary": "", "usage": usage}
            if not batch.calls:
                return {
                    "summary": batch.text or self._last_output(local),
                    "tool_results": local["tool_results"],
                    "usage": usage,
                }
            if any(call.get("name") not in allowed for call in batch.calls):
                return {
                    "error": "subagent requested a tool outside its read-only allowlist",
                    "usage": usage,
                }
            results = self.registry.execute_many(
                batch.calls,
                lambda: ToolContext(
                    mode="readonly",
                    allowed_roots=self.config.allowed_roots,
                    state=local,
                    permission_store=self.permissions,
                    services={"max_tool_output_chars": self.config.max_tool_output_chars},
                ),
                max_workers=self.config.max_parallel_tools,
            )
            for call, result in zip(batch.calls, results):
                self._emit_result(call, result)
                local["tool_results"].append(result.model_dump())
                local["messages"].append({"role": "tool", "content": result.to_message()})
            context = f"{SUBAGENT_PROMPT}\n\nTask: {task}\n\nResults:\n{self._last_output(local)}"
        return {
            "error": "subagent iteration limit reached",
            "summary": self._last_output(local),
            "usage": usage,
        }

    @staticmethod
    def _add_usage(total: dict[str, int], usage: dict[str, int]) -> None:
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        total["input_tokens"] = int(total.get("input_tokens", 0)) + input_tokens
        total["output_tokens"] = int(total.get("output_tokens", 0)) + output_tokens
        total["reasoning_tokens"] = int(total.get("reasoning_tokens", 0)) + int(
            usage.get("reasoning_tokens", 0)
        )
        total["total_tokens"] = int(total.get("total_tokens", 0)) + int(
            usage.get("total_tokens", input_tokens + output_tokens)
        )

    def _emit_result(self, call: dict[str, Any], result: ToolResult) -> None:
        self.emit(
            AgentEvent(
                type="item.completed",
                stage="subagent",
                item_type="tool_result",
                call_id=str(call.get("call_id") or ""),
                tool_name=str(call.get("name") or ""),
                arguments=dict(call.get("arguments") or {}),
                result=result.model_dump(),
                payload={"result": result.model_dump()},
            )
        )

    @staticmethod
    def _last_output(state: dict[str, Any]) -> str:
        for result in reversed(state.get("tool_results", [])):
            if result.get("output"):
                return str(result["output"])
        return ""
