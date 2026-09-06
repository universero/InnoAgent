"""Read file tool."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import Field, model_validator

from core.tool.base import BaseTool, ToolContext, ToolInput, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Read a UTF-8 text file inside the workspace. Use this before editing an existing
file and when exact source text is needed. Optionally request a 1-based inclusive line range; prefer
one useful range over many tiny reads. Do not use it to probe paths outside the workspace. The result
includes the resolved path, byte digest, character count, total line count, and returned range."""


class ReadInput(ToolInput):
    """Arguments accepted by the read tool."""
    path: str = Field(min_length=1, description="Workspace-relative or absolute file path to read.")
    start_line: int | None = Field(
        default=None,
        ge=1,
        description="Optional first line to return, using 1-based inclusive numbering.",
    )
    end_line: int | None = Field(
        default=None,
        ge=1,
        description="Optional last line to return, using 1-based inclusive numbering.",
    )

    @model_validator(mode="after")
    def validate_range(self) -> "ReadInput":
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


@tool
class ReadTool(BaseTool):
    """Read file contents, optionally by line range."""
    name = "read"
    description = TOOL_PROMPT
    input_model = ReadInput

    def run(self, tool_input: ReadInput, context: ToolContext) -> ToolResult:
        """Read a file, optionally limiting to a line range."""
        args = tool_input.model_dump()
        path = context.resolve_path(args["path"])
        if not path.exists() or not path.is_file():
            return ToolResult(
                tool_name=self.name,
                status="error",
                output=f"文件不存在: {path}",
            )
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8", errors="replace")
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
                "sha256": hashlib.sha256(raw).hexdigest(),
                "byte_count": len(raw),
                "character_count": len(content),
                "size": len(content),
                "line_count": len(lines),
                "start_line": start,
                "end_line": min(end, len(lines)),
            },
        )
