"""Write file tool."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.decorators import write_tool


TOOL_PROMPT = """Create or overwrite a UTF-8 file inside the workspace. Read an existing target
before writing it and preserve unrelated user changes. This is a mutating operation and may require
runtime approval. Prefer focused edits; do not create documentation unless the user requested it."""


class WriteInput(BaseModel):
    """Arguments accepted by the write tool."""
    path: str
    content: str


@write_tool
class WriteTool(BaseTool):
    """Create or overwrite files."""
    name = "write"
    description = TOOL_PROMPT
    input_model = WriteInput
    is_write = True

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        """Create or overwrite a file."""
        args = tool_input.model_dump()
        path = context.resolve_path(args["path"])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existed = path.exists()
            old_content = path.read_text(encoding="utf-8", errors="replace") if existed else ""
            path.write_text(args["content"], encoding="utf-8")
        except OSError as exc:
            return ToolResult(tool_name=self.name, status="error", output=str(exc))
        return ToolResult(
            tool_name=self.name,
            status="success",
            output=f"已写入 {path} ({len(args['content'])} chars)",
            metadata={
                "path": str(path),
                "existed": existed,
                "old_size": len(old_content),
                "new_size": len(args["content"]),
            },
        )
