"""CLI rendering and command tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.runtime.agent import InnoAgentRuntime
from core.runtime.config import RuntimeConfig
from observe.traces import TraceStore
from test.fakes import FakeModel
from view.cli import InnoAgentCLI
from view.commands import parse_command
from view.render import render_event, render_events, render_sessions, render_state, render_tools


class CliTest(unittest.TestCase):
    """Tests for CLI command and rendering helpers."""

    def test_parse_slash_command(self) -> None:
        """Verify slash command parsing."""
        command = parse_command("/mode confirm")
        self.assertIsNotNone(command)
        self.assertEqual(command.name, "mode")
        self.assertEqual(command.args, ["confirm"])

    def test_render_state(self) -> None:
        """Verify final state rendering omits internal status text."""
        text = render_state(
            {
                "response": "done",
                "plan": {"status": "active", "revision": 1, "steps": []},
                "tasks": [{"title": "a", "status": "done"}],
                "goal_complete": True,
            }
        )
        self.assertIn("done", text)
        self.assertNotIn("目标已完成", text)

    def test_render_state_falls_back_to_tool_result_when_response_empty(self) -> None:
        """Verify tool output is shown when the final response is empty."""
        text = render_state(
            {
                "response": "",
                "tool_results": [
                    {
                        "tool_name": "read",
                        "status": "success",
                        "output": "its a test",
                    }
                ],
            }
        )
        self.assertIn("its a test", text)

    def test_render_state_skips_already_streamed_response(self) -> None:
        """Verify streamed response text is not duplicated."""
        text = render_state(
            {
                "response": "its a test",
                "streamed_response": "its a test",
                "tool_results": [],
            }
        )
        self.assertNotIn("its a test", text)

    def test_render_tool_use_fields(self) -> None:
        """Verify legacy tool-use trace rendering."""
        tracer = TraceStore()
        tracer.record(
            "tool_use",
            {
                "tool_name": "read",
                "function": 'read(path="README.md")',
                "arguments": {"path": "README.md"},
                "status": "success",
                "summary": "读取成功",
                "warnings": [],
            },
        )
        lines = render_events(tracer, 0)
        self.assertTrue(any("名称：read" in line for line in lines))
        self.assertTrue(any("函数：read" in line for line in lines))
        self.assertTrue(any("读取成功" in line for line in lines))

    def test_render_event_tool_uses_name_params_and_result(self) -> None:
        """Verify tool events render name, params and result."""
        call_text = render_event(
            {
                "type": "tool",
                "phase": "call",
                "tool_name": "read",
                "arguments": {"path": "test.md"},
            }
        )
        self.assertIn("名称：read", call_text)
        self.assertIn("参数：path=test.md", call_text)
        self.assertNotIn("准备调用", call_text)

    def test_cli_goal_and_resume(self) -> None:
        """Verify a full CLI task, rename and resume path."""
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                memory_enabled=False,
            )
            runtime = InnoAgentRuntime(config, model=FakeModel())
            output: list[str] = []
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=output.append)
            cli._handle_command(parse_command("/goal create file"))
            self.assertEqual(cli.current_goal, "create file")
            cli._handle_task("创建 app.py")
            self.assertIsNotNone(cli.current_session_id)
            cli._handle_command(parse_command("/rename 项目初始化"))
            self.assertTrue(any("已重命名为" in line for line in output))
            cli._handle_command(parse_command("/resume"))
            self.assertTrue(any("已恢复" in line for line in output))


if __name__ == "__main__":
    unittest.main()
