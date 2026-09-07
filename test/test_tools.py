"""Tool registry and guardrail tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.guardrails.base import BaseGuardrail
from core.tool.base import ToolAuthorization, ToolContext, ToolSpec
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

    def test_authorization_is_separate_from_execution(self) -> None:
        registry = ToolRegistry()
        registry.set_default_guardrails()
        registry.register(WriteTool())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            authorization = registry.authorize_tool(
                "write",
                {"path": str(path), "content": "x"},
                ToolContext(mode="ask", allowed_roots=[tmp]),
            )

            self.assertEqual(authorization.status, "needs_confirmation")
            self.assertFalse(path.exists())

    def test_invalid_arguments_fail_before_approval(self) -> None:
        registry = ToolRegistry()
        registry.set_default_guardrails()
        registry.register(WriteTool())

        authorization = registry.authorize_tool(
            "write",
            {"path": "app.py", "content": "x", "unexpected": True},
            ToolContext(mode="ask", allowed_roots=["."]),
        )

        self.assertEqual(authorization.status, "error")
        self.assertIn("extra_forbidden", authorization.reason)

    def test_authorized_execution_cannot_switch_tools(self) -> None:
        registry = ToolRegistry()
        registry.register(ReadTool())
        registry.register(WriteTool())
        authorization = ToolAuthorization(
            tool_name="read",
            status="allowed",
            arguments={"path": "test.md"},
        )

        result = registry.execute_authorized(
            "write",
            {"path": "changed.md", "content": "unsafe"},
            ToolContext(mode="auto", allowed_roots=["."]),
            authorization,
        )

        self.assertEqual(result.status, "error")
        self.assertIn("does not match", result.output)

    def test_authorized_execution_uses_bound_arguments(self) -> None:
        registry = ToolRegistry()
        registry.register(ReadTool())
        with tempfile.TemporaryDirectory() as tmp:
            approved = Path(tmp) / "approved.txt"
            changed = Path(tmp) / "changed.txt"
            approved.write_text("approved", encoding="utf-8")
            changed.write_text("changed", encoding="utf-8")
            context = ToolContext(mode="auto", allowed_roots=[tmp])
            authorization = registry.authorize_tool(
                "read",
                {"path": str(approved)},
                context,
            )

            result = registry.execute_authorized(
                "read",
                {"path": str(changed)},
                context,
                authorization,
            )

            self.assertEqual(result.status, "success")
            self.assertIn("approved", result.output)
            self.assertNotIn("changed", result.output)

    def test_authorization_binds_resolved_path_before_symlink_changes(self) -> None:
        registry = ToolRegistry()
        registry.set_default_guardrails()
        registry.register(ReadTool())
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            safe = Path(tmp) / "safe"
            safe.mkdir()
            (safe / "value.txt").write_text("safe", encoding="utf-8")
            (Path(outside) / "value.txt").write_text("outside", encoding="utf-8")
            link = Path(tmp) / "link"
            try:
                link.symlink_to(safe, target_is_directory=True)
            except OSError:
                self.skipTest("symbolic links are not available")
            context = ToolContext(mode="auto", allowed_roots=[tmp])
            authorization = registry.authorize_tool(
                "read",
                {"path": str(link / "value.txt")},
                context,
            )
            link.unlink()
            link.symlink_to(outside, target_is_directory=True)

            result = registry.execute_authorized(
                "read",
                {"path": str(link / "value.txt")},
                context,
                authorization,
            )

            self.assertEqual(
                authorization.arguments["path"],
                str((safe / "value.txt").resolve()),
            )
            self.assertEqual(result.output, "safe")

    def test_guardrail_preflight_failure_is_closed(self) -> None:
        class FailingGuardrail(BaseGuardrail):
            name = "failing"

            def before(self, tool, arguments, context):
                raise RuntimeError("policy unavailable")

        registry = ToolRegistry()
        registry.register(ReadTool())
        registry.register_guardrail(FailingGuardrail())

        authorization = registry.authorize_tool(
            "read",
            {"path": "test.md"},
            ToolContext(mode="auto", allowed_roots=["."]),
        )

        self.assertEqual(authorization.status, "error")
        self.assertIn("policy unavailable", authorization.reason)

    def test_guardrail_postflight_failure_marks_effect_uncertain(self) -> None:
        class FailingGuardrail(BaseGuardrail):
            name = "failing"

            def after(self, tool, result, context):
                raise RuntimeError("audit unavailable")

        registry = ToolRegistry()
        registry.register(WriteTool())
        registry.register_guardrail(FailingGuardrail())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "written.txt"
            result = registry.execute_tool(
                "write",
                {"path": str(path), "content": "written"},
                ToolContext(mode="auto", allowed_roots=[tmp]),
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "written")
            self.assertEqual(result.status, "error")
            self.assertTrue(result.metadata["effect_uncertain"])

    def test_execute_many_rejects_misaligned_authorizations(self) -> None:
        registry = ToolRegistry()
        registry.register(ReadTool())

        with self.assertRaisesRegex(ValueError, "authorizations must match calls"):
            registry.execute_many(
                [{"name": "read", "arguments": {"path": "test.md"}}],
                lambda: ToolContext(mode="auto", allowed_roots=["."]),
                authorizations=[],
            )

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

    def test_read_streams_large_files_into_a_bounded_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.txt"
            path.write_text("0123456789\n" * 1000, encoding="utf-8")

            result = ReadTool().execute(
                {"path": str(path)},
                ToolContext(
                    mode="auto",
                    allowed_roots=[tmp],
                    services={"max_tool_output_chars": 64},
                ),
            )

            self.assertTrue(result.metadata["truncated"])
            self.assertIn("tool output was truncated", result.warnings)
            self.assertLessEqual(len(result.output.split("\n[...", 1)[0]), 64)
            self.assertEqual(result.metadata["line_count"], 1000)

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

    def test_tool_specs_expose_trusted_execution_capabilities(self) -> None:
        registry = ToolRegistry()
        registry.register(ReadTool())
        registry.register(WriteTool())

        specs = {spec.name: spec for spec in registry.tool_specs()}

        self.assertIsInstance(specs["read"], ToolSpec)
        self.assertTrue(specs["read"].parallel_safe)
        self.assertFalse(specs["read"].is_write)
        self.assertTrue(specs["write"].is_write)

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

    def test_grep_stops_capturing_at_character_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "large.txt").write_text("match " + "x" * 5000, encoding="utf-8")

            result = GrepTool().execute(
                {"pattern": "match", "path": tmp},
                ToolContext(
                    mode="auto",
                    allowed_roots=[tmp],
                    services={"max_tool_output_chars": 80},
                ),
            )

            self.assertTrue(result.metadata["truncated"])
            self.assertIn("搜索结果已截断", result.warnings)
            self.assertLessEqual(len(result.output), 80)

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

    def test_ls_stops_traversal_at_entry_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(20):
                Path(tmp, f"item-{index:02d}.txt").write_text("x", encoding="utf-8")

            result = LsTool().execute(
                {"path": tmp, "max_entries": 3},
                ToolContext(
                    mode="auto",
                    allowed_roots=[tmp],
                    services={"max_tool_output_chars": 1000},
                ),
            )

            self.assertEqual(result.metadata["count"], 3)
            self.assertTrue(result.metadata["truncated"])
            self.assertIn("tool output was truncated", result.warnings)

    def test_ls_stops_traversal_at_character_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a-very-long-file-name.txt").write_text("x", encoding="utf-8")

            result = LsTool().execute(
                {"path": tmp},
                ToolContext(
                    mode="auto",
                    allowed_roots=[tmp],
                    services={"max_tool_output_chars": 8},
                ),
            )

            retained = result.output.split("\n[...", 1)[0]
            self.assertLessEqual(len(retained), 8)
            self.assertTrue(result.metadata["truncated"])

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

    def test_shell_captures_stdout_and_stderr_with_hard_memory_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = ShellTool().execute(
                {
                    "command": (
                        "python3 -c \"import sys; "
                        "print('x' * 10000); print('y' * 10000, file=sys.stderr)\""
                    )
                },
                ToolContext(
                    mode="auto",
                    allowed_roots=[tmp],
                    services={"max_tool_output_chars": 128},
                ),
            )

            self.assertEqual(result.status, "success")
            self.assertLessEqual(len(result.data["stdout"].encode()), 128)
            self.assertLessEqual(len(result.data["stderr"].encode()), 128)
            self.assertTrue(result.metadata["output_truncated"])
            self.assertIn("tool output was truncated", result.warnings)


if __name__ == "__main__":
    unittest.main()
