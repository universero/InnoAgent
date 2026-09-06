"""Shell command execution tool."""

from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field

from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Execute one shell command in the workspace and return stdout, stderr, and exit
code. Use dedicated read/ls/grep/write tools when they are sufficient. Avoid interactive commands,
background daemons, destructive operations, and commands that expose secrets. Shell execution is a
mutating capability and is subject to runtime approval in ask mode."""


class ShellInput(BaseModel):
    command: str
    timeout_seconds: int = Field(default=60, ge=1, le=600)


@tool
class ShellTool(BaseTool):
    name = "shell"
    description = TOOL_PROMPT
    input_model = ShellInput
    is_write = True
    requires_confirmation = True
    parallel_safe = False

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        args = tool_input.model_dump()
        cwd = context.allowed_roots[0] if context.allowed_roots else None
        try:
            completed = subprocess.run(
                args["command"],
                shell=True,
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=args["timeout_seconds"],
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_name=self.name,
                status="error",
                output=f"command timed out after {args['timeout_seconds']}s",
                metadata={"command": args["command"], "timeout": True},
            )
        output_parts = []
        if completed.stdout:
            output_parts.append(completed.stdout.rstrip())
        if completed.stderr:
            output_parts.append(completed.stderr.rstrip())
        output = "\n".join(output_parts) or f"process exited with code {completed.returncode}"
        return ToolResult(
            tool_name=self.name,
            status="success" if completed.returncode == 0 else "error",
            output=output,
            metadata={
                "command": args["command"],
                "cwd": cwd,
                "exit_code": completed.returncode,
            },
        )
