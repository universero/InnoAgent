"""Tool registry and guardrail tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.tool.base import ToolContext
from core.tool.registry import ToolRegistry
from core.tool.read_tool import ReadTool
from core.tool.write_tool import WriteTool


class ToolRegistryTest(unittest.TestCase):
    """Tests for built-in tools and guardrail enforcement."""

    def test_write_is_blocked_in_readonly_mode(self) -> None:
        """Verify writes are blocked in readonly mode."""
        registry = ToolRegistry()
        registry.set_default_guardrails()
        registry.register(WriteTool())
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(mode="readonly", allowed_roots=[tmp])
            result = registry.execute_tool("write", {"path": str(Path(tmp) / "a.txt"), "content": "x"}, context)
            self.assertEqual(result.status, "blocked")

    def test_write_requires_confirmation_in_confirm_mode(self) -> None:
        """Verify writes require confirmation in confirm mode."""
        registry = ToolRegistry()
        registry.set_default_guardrails()
        registry.register(WriteTool())
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(mode="confirm", allowed_roots=[tmp])
            result = registry.execute_tool("write", {"path": str(Path(tmp) / "a.txt"), "content": "x"}, context)
            self.assertEqual(result.status, "needs_confirmation")

    def test_write_before_read_is_recorded(self) -> None:
        """Verify write-before-read metadata is recorded."""
        registry = ToolRegistry()
        registry.set_default_guardrails()
        registry.register(WriteTool())
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(mode="auto", allowed_roots=[tmp])
            result = registry.execute_tool("write", {"path": str(Path(tmp) / "a.txt"), "content": "x"}, context)
            self.assertEqual(result.status, "success")
            self.assertIn("guardrails_before", result.metadata)
            self.assertIn("pre_read", result.metadata["guardrails_before"]["write_before_read"])

    def test_read_tool(self) -> None:
        """Verify the read tool returns file contents."""
        registry = ToolRegistry()
        registry.set_default_guardrails()
        registry.register(ReadTool())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_text("hello", encoding="utf-8")
            context = ToolContext(mode="auto", allowed_roots=[tmp])
            result = registry.execute_tool("read", {"path": str(path)}, context)
            self.assertEqual(result.status, "success")
            self.assertIn("hello", result.output)

    def test_unknown_tool_returns_model_visible_error(self) -> None:
        registry = ToolRegistry()
        results = registry.execute_many(
            [{"name": "missing", "arguments": {}}],
            lambda: ToolContext(mode="auto"),
        )
        self.assertEqual(results[0].status, "error")
        self.assertIn("unknown tool", results[0].output)


if __name__ == "__main__":
    unittest.main()
