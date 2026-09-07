"""List directory tool."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field

from core.tool.base import BaseTool, ToolContext, ToolInput, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """List files and directories inside the workspace. Start with a targeted directory.
Use recursive mode only when bounded traversal is necessary, and keep depth small instead of dumping
the repository. Results stop at max_entries or the runtime output budget. Symbolic-link directories
are listed but never traversed."""


class LsInput(ToolInput):
    """Arguments accepted by the ls tool."""
    path: str = Field(default=".", min_length=1, description="Workspace directory to list.")
    recursive: bool = Field(default=False, description="Whether to recurse into child directories.")
    depth: int = Field(default=1, ge=1, le=5, description="Maximum recursive depth, from 1 to 5.")
    max_entries: int = Field(
        default=1000,
        ge=1,
        le=5000,
        description="Maximum number of paths returned before traversal stops.",
    )


@tool
class LsTool(BaseTool):
    """List files and directories."""
    name = "ls"
    description = TOOL_PROMPT
    input_model = LsInput

    def run(self, tool_input: LsInput, context: ToolContext) -> ToolResult:
        """List directory entries, optionally recursively."""
        args = tool_input.model_dump()
        root = context.resolve_path(args["path"])
        if not root.exists() or not root.is_dir():
            return ToolResult(
                tool_name=self.name,
                status="error",
                output=f"目录不存在: {root}",
            )
        max_chars = max(1, int(context.services.get("max_tool_output_chars", 30000)))
        entries, truncated = self._list(
            root,
            args["recursive"],
            args["depth"],
            max_entries=args["max_entries"],
            max_chars=max_chars,
        )
        output = "\n".join(entries) or "(empty)"
        if truncated:
            output += "\n[... directory listing truncated]"
        return ToolResult(
            tool_name=self.name,
            status="success",
            output=output,
            warnings=["tool output was truncated"] if truncated else [],
            metadata={
                "path": str(root),
                "count": len(entries),
                "truncated": truncated,
                "max_entries": args["max_entries"],
            },
        )

    def _list(
        self,
        root: Path,
        recursive: bool,
        depth: int,
        *,
        max_entries: int,
        max_chars: int,
    ) -> tuple[list[str], bool]:
        """Scan a directory without retaining an unbounded entry list."""
        results: list[str] = []
        output_chars = 0

        def visit(directory: Path, prefix: str, remaining_depth: int) -> bool:
            nonlocal output_chars
            with os.scandir(directory) as scanner:
                for entry in scanner:
                    if len(results) >= max_entries:
                        return False
                    relative = f"{prefix}/{entry.name}" if prefix else entry.name
                    separator_size = 1 if results else 0
                    remaining_chars = max_chars - output_chars - separator_size
                    if remaining_chars <= 0:
                        return False
                    if len(relative) > remaining_chars:
                        results.append(relative[:remaining_chars])
                        output_chars = max_chars
                        return False
                    results.append(relative)
                    output_chars += separator_size + len(relative)
                    if (
                        recursive
                        and remaining_depth > 1
                        and entry.is_dir(follow_symlinks=False)
                    ):
                        if not visit(Path(entry.path), relative, remaining_depth - 1):
                            return False
            return True

        completed = visit(root, "", depth)
        return results, not completed
