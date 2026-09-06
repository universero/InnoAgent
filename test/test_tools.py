"""Tool registry and guardrail tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.tool.base import ToolContext
from core.tool.grep_tool import GrepTool
from core.tool.ls_tool import LsTool
from core.tool.plan_tool import PlanTool
from core.tool.shell_tool import ShellTool
from core.tool.skill_tool import SkillTool
from core.tool.subagent_tool import SubagentTool
from core.tool.task_tool import TaskTool
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

    def test_tool_schemas_describe_every_argument_and_reject_unknown_fields(self) -> None:
        registry = ToolRegistry()
        for tool in [
            ReadTool(),
            WriteTool(),
            LsTool(),
            GrepTool(),
            ShellTool(),
            PlanTool(),
            TaskTool(),
            SkillTool(),
            SubagentTool(),
        ]:
            registry.register(tool)

        for schema in registry.tool_schemas():
            parameters = schema["parameters"]
            self.assertFalse(parameters.get("additionalProperties", True), schema["name"])
            for name, value in parameters.get("properties", {}).items():
                self.assertTrue(value.get("description"), f"{schema['name']}.{name}")

        result = registry.execute_tool(
            "read",
            {"path": "missing.txt", "unexpected": True},
            ToolContext(mode="auto", allowed_roots=["."]),
        )
        self.assertEqual(result.status, "error")
        self.assertIn("extra_forbidden", result.output)

    def test_read_rejects_reversed_line_range(self) -> None:
        result = ReadTool().execute(
            {"path": "a.txt", "start_line": 3, "end_line": 2},
            ToolContext(mode="auto", allowed_roots=["."]),
        )
        self.assertEqual(result.status, "error")
        self.assertIn("end_line", result.output)

    def test_subagent_rejects_write_capable_tool_allowlist(self) -> None:
        result = SubagentTool().execute(
            {"task": "inspect the implementation", "allowed_tools": ["read", "shell"]},
            ToolContext(mode="auto"),
        )
        self.assertEqual(result.status, "error")
        self.assertIn("allowed_tools", result.output)

    def test_task_update_requires_explicit_status(self) -> None:
        result = TaskTool().execute(
            {"task_id": "task-1"},
            ToolContext(mode="auto"),
        )
        self.assertEqual(result.status, "error")
        self.assertIn("status", result.output)

    def test_write_rejects_stale_expected_digest(self) -> None:
        registry = ToolRegistry()
        registry.set_default_guardrails()
        registry.register(ReadTool())
        registry.register(WriteTool())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_text("first", encoding="utf-8")
            context = ToolContext(mode="auto", allowed_roots=[tmp])
            read = registry.execute_tool("read", {"path": str(path)}, context)
            path.write_text("external", encoding="utf-8")
            result = registry.execute_tool(
                "write",
                {
                    "path": str(path),
                    "content": "agent",
                    "expected_sha256": read.metadata["sha256"],
                },
                context,
            )
            self.assertEqual(result.status, "error")
            self.assertEqual(path.read_text(encoding="utf-8"), "external")

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not portable to Windows")
    def test_atomic_write_preserves_existing_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.sh"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o750)
            result = WriteTool().execute(
                {"path": str(path), "content": "new"},
                ToolContext(mode="auto", allowed_roots=[tmp]),
            )
            self.assertEqual(result.status, "success")
            self.assertEqual(path.stat().st_mode & 0o777, 0o750)

    def test_grep_handles_leading_dash_and_caps_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_text("-flag\n-flag\n-flag\n", encoding="utf-8")
            result = GrepTool().execute(
                {"pattern": "-flag", "path": tmp, "fixed_strings": True, "max_results": 2},
                ToolContext(mode="auto", allowed_roots=[tmp]),
            )
            self.assertEqual(result.status, "success")
            self.assertEqual(result.metadata["match_count"], 3)
            self.assertEqual(result.metadata["returned_count"], 2)
            self.assertTrue(result.warnings)

    def test_recursive_ls_does_not_follow_symlink_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            Path(outside, "secret.txt").write_text("secret", encoding="utf-8")
            try:
                Path(tmp, "linked").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symbolic links are not available")
            result = LsTool().execute(
                {"path": tmp, "recursive": True, "depth": 3},
                ToolContext(mode="auto", allowed_roots=[tmp]),
            )
            self.assertEqual(result.status, "success")
            self.assertIn("linked", result.output)
            self.assertNotIn("secret.txt", result.output)

    def test_shell_timeout_reports_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = ShellTool().execute(
                {"command": "printf 'started\\n'; sleep 2", "timeout_seconds": 1},
                ToolContext(mode="auto", allowed_roots=[tmp]),
            )
            self.assertEqual(result.status, "error")
            self.assertTrue(result.metadata["timeout"])
            self.assertIsNotNone(result.metadata["exit_code"])
            self.assertIn("started", result.output)
            self.assertIn("started", result.data["stdout"])


if __name__ == "__main__":
    unittest.main()
