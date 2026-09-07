"""Search file contents tool."""

from __future__ import annotations

import subprocess
import tempfile
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
            lines, truncated = self._ripgrep(root, args, context)
        except FileNotFoundError:
            lines, truncated = self._fallback_grep(root, args, context)
        return ToolResult(
            tool_name=self.name,
            status="success",
            output="\n".join(lines) or "无匹配结果",
            warnings=["搜索结果已截断"] if truncated else [],
            metadata={
                "path": str(root),
                "pattern": args["pattern"],
                "match_count": len(lines) + (1 if truncated else 0),
                "returned_count": min(len(lines), args["max_results"]),
                "truncated": truncated,
            },
        )

    def _ripgrep(
        self,
        root: Path,
        args: dict,
        context: ToolContext,
    ) -> tuple[list[str], bool]:
        """Run ripgrep if available."""
        command = ["rg", "--line-number", "--no-heading"]
        if args.get("ignore_case"):
            command.append("--ignore-case")
        if args.get("fixed_strings"):
            command.append("--fixed-strings")
        if args.get("glob"):
            command.extend(["--glob", args["glob"]])
        command.extend(["--", args["pattern"], str(root)])
        return self._run_bounded(command, args["max_results"], context)

    def _fallback_grep(
        self,
        root: Path,
        args: dict,
        context: ToolContext,
    ) -> tuple[list[str], bool]:
        """Run grep when ripgrep is unavailable."""
        command = ["grep", "-rn"]
        if args.get("ignore_case"):
            command.append("-i")
        if args.get("fixed_strings"):
            command.append("-F")
        if args.get("glob"):
            command.extend(["--include", args["glob"]])
        command.extend(["--", args["pattern"], str(root)])
        return self._run_bounded(command, args["max_results"], context)

    @staticmethod
    def _run_bounded(
        command: list[str],
        max_results: int,
        context: ToolContext,
    ) -> tuple[list[str], bool]:
        """Drain search output incrementally and stop after the configured limits."""
        max_chars = int(context.services.get("max_tool_output_chars", 30000))
        lines: list[str] = []
        chars = 0
        truncated = False
        with tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if len(lines) >= max_results:
                    truncated = True
                    process.terminate()
                    break
                separator = 1 if lines else 0
                remaining = max(0, max_chars - chars - separator)
                if len(line) > remaining:
                    if remaining:
                        lines.append(line[:remaining])
                    truncated = True
                    process.terminate()
                    break
                lines.append(line)
                chars += len(line) + separator
            return_code = process.wait()
            process.stdout.close()
            if not truncated and return_code not in {0, 1}:
                stderr_file.seek(0)
                error = stderr_file.read(4096).decode("utf-8", errors="replace").strip()
                raise OSError(error or f"search exited with code {return_code}")
        return lines, truncated
