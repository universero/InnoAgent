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
        start = max(1, args.get("start_line") or 1)
        end = args.get("end_line")
        max_chars = int(context.services.get("max_tool_output_chars", 30000))
        digest = hashlib.sha256()
        byte_count = 0
        character_count = 0
        line_count = 0
        selected_count = 0
        selected_chars = 0
        output_parts: list[str] = []
        output_chars = 0
        try:
            # 逐行扫描以计算完整摘要，但只在内存中保留有界返回内容。
            with path.open("rb") as handle:
                for line_count, raw_line in enumerate(handle, 1):
                    digest.update(raw_line)
                    byte_count += len(raw_line)
                    line = raw_line.decode("utf-8", errors="replace")
                    character_count += len(line)
                    if line_count < start or (end is not None and line_count > end):
                        continue
                    value = line.rstrip("\r\n")
                    prefix = "\n" if selected_count else ""
                    selected_count += 1
                    selected_chars += len(prefix) + len(value)
                    remaining = max(0, max_chars - output_chars)
                    if remaining:
                        chunk = (prefix + value)[:remaining]
                        output_parts.append(chunk)
                        output_chars += len(chunk)
        except OSError as exc:
            return ToolResult(tool_name=self.name, status="error", output=str(exc))

        output = "".join(output_parts)
        omitted = max(0, selected_chars - len(output))
        warnings = []
        if omitted:
            output += f"\n[... {omitted} characters truncated]"
            warnings.append("tool output was truncated")
        return ToolResult(
            tool_name=self.name,
            status="success",
            output=output,
            warnings=warnings,
            metadata={
                "path": str(path),
                "sha256": digest.hexdigest(),
                "byte_count": byte_count,
                "character_count": character_count,
                "size": character_count,
                "line_count": line_count,
                "start_line": start,
                "end_line": min(end or line_count, line_count),
                "truncated": bool(omitted),
            },
        )
