"""Read file tool."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.decorators import tool


class ReadInput(BaseModel):
    """Arguments accepted by the read tool."""
    path: str
    start_line: int | None = None
    end_line: int | None = None


@tool
class ReadTool(BaseTool):
    """Read file contents, optionally by line range."""
    name = "read"
    description = "读取文件内容，可指定起始和结束行。"
    input_model = ReadInput

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        """Read a file, optionally limiting to a line range."""
        args = tool_input.model_dump()
        path = context.resolve_path(args["path"])
        if not path.exists() or not path.is_file():
            return ToolResult(tool_name=self.name, status="error", output=f"文件不存在: {path}")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(tool_name=self.name, status="error", output=str(exc))

        lines = content.splitlines()
        start = max(1, args.get("start_line") or 1)
        end = args.get("end_line") or len(lines)
        selected = lines[start - 1 : end]
        return ToolResult(
            tool_name=self.name,
            status="success",
            output="\n".join(selected),
            metadata={
                "path": str(path),
                "size": len(content),
                "line_count": len(lines),
                "start_line": start,
                "end_line": min(end, len(lines)),
            },
        )
