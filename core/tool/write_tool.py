"""Write file tool."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from pydantic import Field

from core.tool.base import BaseTool, ToolContext, ToolInput, ToolResult
from core.tool.decorators import write_tool


TOOL_PROMPT = """Create or replace one UTF-8 file inside the workspace. Inspect an existing target
before writing, preserve unrelated user changes, and provide expected_sha256 when stale-write
protection matters. The replacement is atomic, but this tool rewrites the whole file; keep edits
focused. This is a mutating operation and may require runtime approval."""


class WriteInput(ToolInput):
    """Arguments accepted by the write tool."""
    path: str = Field(min_length=1, description="Workspace-relative or absolute target file path.")
    content: str = Field(description="Complete UTF-8 content that will replace the target file.")
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Optional SHA-256 returned by read; reject the write if the file changed.",
    )


@write_tool
class WriteTool(BaseTool):
    """Create or overwrite files."""
    name = "write"
    description = TOOL_PROMPT
    input_model = WriteInput
    is_write = True

    def run(self, tool_input: WriteInput, context: ToolContext) -> ToolResult:
        """Create or overwrite a file."""
        args = tool_input.model_dump()
        path = context.resolve_path(args["path"])
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                old_bytes = path.read_bytes()
            except FileNotFoundError:
                existed = False
                old_bytes = b""
                old_digest = None
            else:
                existed = True
                old_digest = hashlib.sha256(old_bytes).hexdigest()
            expected = args.get("expected_sha256")
            if expected and expected.lower() != old_digest:
                return ToolResult(
                    tool_name=self.name,
                    status="error",
                    output="target changed since it was read; read the file again before writing",
                    metadata={
                        "path": str(path),
                        "expected_sha256": expected,
                        "actual_sha256": old_digest,
                    },
                )

            # 同目录临时文件加原子替换，避免异常退出留下半写入文件。
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(args["content"])
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            if existed:
                os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
            if expected:
                try:
                    current_bytes = path.read_bytes()
                except FileNotFoundError:
                    current_digest = None
                else:
                    current_digest = hashlib.sha256(current_bytes).hexdigest()
                if expected.lower() != current_digest:
                    return ToolResult(
                        tool_name=self.name,
                        status="error",
                        output="target changed while preparing the write; read the file again",
                        metadata={
                            "path": str(path),
                            "expected_sha256": expected,
                            "actual_sha256": current_digest,
                        },
                    )
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError as exc:
            return ToolResult(tool_name=self.name, status="error", output=str(exc))
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        new_bytes = args["content"].encode("utf-8")
        return ToolResult(
            tool_name=self.name,
            status="success",
            output=f"已写入 {path} ({len(args['content'])} chars)",
            metadata={
                "path": str(path),
                "existed": existed,
                "old_size": len(old_bytes),
                "new_size": len(new_bytes),
                "character_count": len(args["content"]),
                "sha256": hashlib.sha256(new_bytes).hexdigest(),
            },
        )
