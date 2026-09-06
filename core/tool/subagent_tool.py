"""Isolated subagent delegation tool."""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Delegate a focused, independently solvable task to an isolated subagent. Use it
for bounded research or verification that would otherwise consume substantial parent context.
Provide a precise deliverable and the smallest necessary read-only tool allowlist. The subagent
cannot delegate again."""


class SubagentInput(BaseModel):
    task: str
    role: str = "researcher"
    allowed_tools: list[str] = Field(default_factory=lambda: ["read", "ls", "grep"])


@tool
class SubagentTool(BaseTool):
    name = "subagent"
    description = TOOL_PROMPT
    input_model = SubagentInput

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        runner = context.services.get("subagent_runner")
        if runner is None:
            return ToolResult(tool_name=self.name, status="error", output="subagent runner unavailable")
        result = runner(
            task=tool_input.task,
            role=tool_input.role,
            allowed_tools=tool_input.allowed_tools,
        )
        return ToolResult(
            tool_name=self.name,
            status="success" if not result.get("error") else "error",
            output=str(result.get("summary") or result.get("error") or ""),
            data=result,
        )
