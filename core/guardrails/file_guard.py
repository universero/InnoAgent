"""Path and file-operation guardrails.

The implementation is deliberately conservative: read and write operations
must stay inside the configured allowed roots.  Write operations additionally
enforce ``read-before-write`` so the runtime can show what would be changed and
can support future diff/rollback features.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.guardrails.base import BaseGuardrail, GuardrailDecision
from core.guardrails.policy import is_readonly, should_confirm_write
from core.tool.base import BaseTool, ToolContext, ToolResult


def _resolve(path: str) -> Path:
    """Resolve a user-provided path without using the tool context."""
    return Path(path).expanduser().resolve()


def _is_within(path: Path, roots: list[str]) -> bool:
    """Check whether a path is inside one of the allowed roots."""
    if not roots:
        return True
    return any(path == Path(root).resolve() or Path(root).resolve() in path.parents for root in roots)


def _read_before_write(path: Path) -> dict[str, Any]:
    """Read existing content or inspect the parent directory."""
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return {"target_exists": True, "pre_read_error": str(exc)}
        return {
            "target_exists": True,
            "pre_read_type": "file",
            "pre_read_content": content[:2000],
            "pre_read_size": len(content),
        }

    parent = path.parent
    try:
        entries = sorted(p.name for p in parent.iterdir()) if parent.exists() else []
    except Exception as exc:  # noqa: BLE001
        return {"target_exists": False, "pre_read_error": str(exc)}
    return {
        "target_exists": False,
        "pre_read_type": "directory",
        "parent": str(parent),
        "parent_entries": entries[:200],
    }


class PathGuard(BaseGuardrail):
    """Reject paths outside the allowed roots."""

    name = "path"

    def before(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> GuardrailDecision:
        """Reject paths outside the allowed workspace roots."""
        raw_path = arguments.get("path")
        if not raw_path:
            return GuardrailDecision()

        resolved = context.resolve_path(str(raw_path))
        if not _is_within(resolved, context.allowed_roots):
            return GuardrailDecision(
                allowed=False,
                status="blocked",
                reason=f"path is outside allowed roots: {resolved}",
            )
        return GuardrailDecision(metadata={"resolved_path": str(resolved)})


class WriteBeforeReadGuard(BaseGuardrail):
    """Ensure every write tool observes the target before changing it."""

    name = "write_before_read"

    def before(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> GuardrailDecision:
        """Collect target state before a write operation."""
        if not tool.is_write:
            return GuardrailDecision()

        raw_path = arguments.get("path")
        if not raw_path:
            return GuardrailDecision()
        resolved = context.resolve_path(str(raw_path))
        pre_read = _read_before_write(resolved)
        return GuardrailDecision(metadata={"pre_read": pre_read})


class FileConfirmationGuard(BaseGuardrail):
    """Request confirmation for writes in ``confirm`` mode."""

    name = "file_confirmation"

    def before(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> GuardrailDecision:
        """Request confirmation for writes in confirm mode."""
        if self._is_approved(tool.name, arguments, context.approved_tool_calls):
            return GuardrailDecision()
        if is_readonly(context.mode) and tool.is_write:
            return GuardrailDecision(
                allowed=False,
                status="blocked",
                reason="write operation is not allowed in readonly mode",
            )
        if should_confirm_write(context.mode, tool.is_write):
            return GuardrailDecision(
                allowed=False,
                status="needs_confirmation",
                reason="write operation requires user confirmation in confirm mode",
                metadata={"arguments": arguments},
            )
        return GuardrailDecision()

    @staticmethod
    def _is_approved(
        tool_name: str,
        arguments: dict[str, Any],
        approved: list[dict[str, Any]],
    ) -> bool:
        """Return whether the current call matches an approved confirmation."""
        return any(
            item.get("name") == tool_name and item.get("arguments") == arguments
            for item in approved
        )

    def after(
        self,
        tool: BaseTool,
        result: ToolResult,
        context: ToolContext,
    ) -> ToolResult:
        """Return the tool result unchanged after confirmation checks."""
        return result


class ReadOnlyGuard(BaseGuardrail):
    """Mark read-only context while keeping read tools usable."""

    name = "readonly"

    def before(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> GuardrailDecision:
        """Block write tools when running in readonly mode."""
        if is_readonly(context.mode) and tool.is_write:
            return GuardrailDecision(
                allowed=False,
                status="blocked",
                reason="tool is write-only and mode is readonly",
            )
        return GuardrailDecision()
