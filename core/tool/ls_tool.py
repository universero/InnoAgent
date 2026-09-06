"""List directory tool."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.decorators import tool


class LsInput(BaseModel):
    """Arguments accepted by the ls tool."""
    path: str = "."
    recursive: bool = False
    depth: int = Field(default=1, ge=1, le=5)


@tool
class LsTool(BaseTool):
    """List files and directories."""
    name = "ls"
    description = "列出目录中的文件和子目录。"
    input_model = LsInput

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        """List directory entries, optionally recursively."""
        args = tool_input.model_dump()
        root = context.resolve_path(args["path"])
        if not root.exists() or not root.is_dir():
            return ToolResult(tool_name=self.name, status="error", output=f"目录不存在: {root}")
        entries = self._list(root, args["recursive"], args["depth"])
        return ToolResult(
            tool_name=self.name,
            status="success",
            output="\n".join(entries) or "(empty)",
            metadata={"path": str(root), "count": len(entries)},
        )

    def _list(self, root: Path, recursive: bool, depth: int) -> list[str]:
        """Return relative paths under a directory."""
        if not recursive:
            return sorted(str(item.relative_to(root)) for item in root.iterdir())
        results: list[str] = []
        for item in sorted(root.iterdir(), key=lambda p: p.name):
            rel = str(item.relative_to(root))
            results.append(rel)
            if item.is_dir() and depth > 1:
                results.extend(
                    f"{rel}/{child}" for child in self._list(item, True, depth - 1)
                )
        return results
