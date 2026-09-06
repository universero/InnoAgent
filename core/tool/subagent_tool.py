"""Isolated subagent delegation tool."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from core.tool.base import BaseTool, ToolContext, ToolInput, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Delegate one focused, independently verifiable read-only task to an isolated
subagent. Use it when context isolation or a focused investigation provides clear value, not for work
the main agent can do directly. State the expected deliverable and grant the smallest tool set. The
subagent receives no parent conversation, cannot modify files, and cannot delegate again."""


class SubagentInput(ToolInput):
    task: str = Field(
        min_length=1,
        description="Self-contained delegated task and expected output.",
    )
    role: str = Field(default="researcher", min_length=1, description="Concise specialist role.")
    allowed_tools: list[Literal["read", "ls", "grep"]] = Field(
        default_factory=lambda: ["read", "ls", "grep"],
        min_length=1,
        description="Smallest required subset of read, ls, and grep.",
    )


@tool
class SubagentTool(BaseTool):
    name = "subagent"
    description = TOOL_PROMPT
    input_model = SubagentInput
    parallel_safe = False

    def run(self, tool_input: SubagentInput, context: ToolContext) -> ToolResult:
        runner = context.services.get("subagent_runner")
        if runner is None:
            return ToolResult(
                tool_name=self.name,
                status="error",
                output="subagent runner unavailable",
            )
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
