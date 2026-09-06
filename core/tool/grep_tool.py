"""Search file contents tool."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import Field

from core.tool.base import BaseTool, ToolContext, ToolInput, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Search workspace file contents and return matching paths with line numbers. Use a
regular expression by default or fixed_strings for literal text. Narrow broad searches with path and
glob, and cap results instead of dumping large repositories. Prefer this tool over shell grep;
independent searches may run in parallel."""


class GrepInput(ToolInput):
    """Arguments accepted by the grep tool."""
    pattern: str = Field(min_length=1, description="Regular expression or literal text to find.")
    path: str = Field(default=".", min_length=1, description="Workspace path to search.")
    glob: str | None = Field(default=None, description="Optional file glob, for example '*.py'.")
    ignore_case: bool = Field(default=False, description="Match without case sensitivity.")
    fixed_strings: bool = Field(
        default=False,
        description="Treat pattern as literal text, not regex.",
    )
    max_results: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="Maximum matching lines returned.",
    )


@tool
class GrepTool(BaseTool):
    """Search file contents with ripgrep or grep."""
    name = "grep"
    description = TOOL_PROMPT
    input_model = GrepInput

    def run(self, tool_input: GrepInput, context: ToolContext) -> ToolResult:
        """Search a path, preferring ripgrep and falling back to grep."""
        args = tool_input.model_dump()
        root = context.resolve_path(args["path"])
        if not root.exists():
            return ToolResult(
                tool_name=self.name,
                status="error",
                output=f"路径不存在: {root}",
            )
        try:
            output = self._ripgrep(root, args)
        except FileNotFoundError:
            output = self._fallback_grep(root, args)
        lines = output.splitlines()
        selected = lines[: args["max_results"]]
        return ToolResult(
            tool_name=self.name,
            status="success",
            output="\n".join(selected) or "无匹配结果",
            warnings=["搜索结果已截断"] if len(lines) > len(selected) else [],
            metadata={
                "path": str(root),
                "pattern": args["pattern"],
                "match_count": len(lines),
                "returned_count": len(selected),
            },
        )

    def _ripgrep(self, root: Path, args: dict) -> str:
        """Run ripgrep if available."""
        command = ["rg", "--line-number", "--no-heading"]
        if args.get("ignore_case"):
            command.append("--ignore-case")
        if args.get("fixed_strings"):
            command.append("--fixed-strings")
        if args.get("glob"):
            command.extend(["--glob", args["glob"]])
        command.extend(["--", args["pattern"], str(root)])
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode not in {0, 1}:
            raise OSError(result.stderr.strip())
        return result.stdout.strip()

    def _fallback_grep(self, root: Path, args: dict) -> str:
        """Run grep when ripgrep is unavailable."""
        command = ["grep", "-rn"]
        if args.get("ignore_case"):
            command.append("-i")
        if args.get("fixed_strings"):
            command.append("-F")
        if args.get("glob"):
            command.extend(["--include", args["glob"]])
        command.extend(["--", args["pattern"], str(root)])
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode not in {0, 1}:
            raise OSError(result.stderr.strip())
        return result.stdout.strip()
