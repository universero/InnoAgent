"""Search file contents tool."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Search file contents with a regular expression, returning paths and line numbers.
Use a glob to narrow broad searches. Prefer this tool over shell grep for normal repository search;
independent searches may be called in parallel."""


class GrepInput(BaseModel):
    """Arguments accepted by the grep tool."""
    pattern: str
    path: str = "."
    glob: str | None = None
    ignore_case: bool = False


@tool
class GrepTool(BaseTool):
    """Search file contents with ripgrep or grep."""
    name = "grep"
    description = TOOL_PROMPT
    input_model = GrepInput

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        """Search a path, preferring ripgrep and falling back to grep."""
        args = tool_input.model_dump()
        root = context.resolve_path(args["path"])
        if not root.exists():
            return ToolResult(tool_name=self.name, status="error", output=f"路径不存在: {root}")
        try:
            output = self._ripgrep(root, args)
        except FileNotFoundError:
            output = self._fallback_grep(root, args)
        return ToolResult(
            tool_name=self.name,
            status="success",
            output=output or "无匹配结果",
            metadata={"path": str(root), "pattern": args["pattern"]},
        )

    def _ripgrep(self, root: Path, args: dict) -> str:
        """Run ripgrep if available."""
        command = ["rg", "--line-number", "--no-heading"]
        if args.get("ignore_case"):
            command.append("--ignore-case")
        if args.get("glob"):
            command.extend(["--glob", args["glob"]])
        command.extend([args["pattern"], str(root)])
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode not in {0, 1}:
            raise OSError(result.stderr.strip())
        return result.stdout.strip()

    def _fallback_grep(self, root: Path, args: dict) -> str:
        """Run grep when ripgrep is unavailable."""
        command = ["grep", "-rn"]
        if args.get("ignore_case"):
            command.append("-i")
        if args.get("glob"):
            command.extend(["--include", args["glob"]])
        command.extend([args["pattern"], str(root)])
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode not in {0, 1}:
            raise OSError(result.stderr.strip())
        return result.stdout.strip()
