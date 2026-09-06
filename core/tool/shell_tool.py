"""Shell command execution tool."""

from __future__ import annotations

import os
import signal
import subprocess

from pydantic import Field

from core.tool.base import BaseTool, ToolContext, ToolInput, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Run one non-interactive shell command in the workspace and return combined stdout,
stderr, exit code, and timeout state. Use read, ls, grep, or write when those tools are sufficient.
Do not start interactive programs or background daemons, expose credentials, or run destructive Git
or filesystem commands unless the user explicitly requested them. Shell always uses the runtime
permission policy and is treated as a mutating capability."""


class ShellInput(ToolInput):
    command: str = Field(min_length=1, description="Complete non-interactive shell command to run.")
    timeout_seconds: int = Field(
        default=60,
        ge=1,
        le=600,
        description="Hard execution timeout in seconds, from 1 to 600.",
    )


@tool
class ShellTool(BaseTool):
    name = "shell"
    description = TOOL_PROMPT
    input_model = ShellInput
    is_write = True
    requires_confirmation = True
    parallel_safe = False

    def run(self, tool_input: ShellInput, context: ToolContext) -> ToolResult:
        args = tool_input.model_dump()
        cwd = context.allowed_roots[0] if context.allowed_roots else None
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                args["command"],
                shell=True,
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            stdout, stderr = process.communicate(timeout=args["timeout_seconds"])
        except subprocess.TimeoutExpired:
            if process is not None:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                stdout, stderr = process.communicate()
            output_parts = [f"command timed out after {args['timeout_seconds']}s"]
            if stdout:
                output_parts.append(stdout.rstrip())
            if stderr:
                output_parts.append(stderr.rstrip())
            return_code = process.returncode if process is not None else None
            return ToolResult(
                tool_name=self.name,
                status="error",
                output="\n".join(output_parts),
                data={"stdout": stdout, "stderr": stderr},
                metadata={
                    "command": args["command"],
                    "cwd": cwd,
                    "exit_code": return_code,
                    "timeout": True,
                },
            )
        output_parts = []
        if stdout:
            output_parts.append(stdout.rstrip())
        if stderr:
            output_parts.append(stderr.rstrip())
        return_code = process.returncode if process is not None else -1
        output = "\n".join(output_parts) or f"process exited with code {return_code}"
        return ToolResult(
            tool_name=self.name,
            status="success" if return_code == 0 else "error",
            output=output,
            data={"stdout": stdout, "stderr": stderr},
            metadata={
                "command": args["command"],
                "cwd": cwd,
                "exit_code": return_code,
                "timeout": False,
            },
        )
