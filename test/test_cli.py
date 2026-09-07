"""CLI rendering and command tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.document import Document
from prompt_toolkit.completion import CompleteEvent

from core.runtime.agent import InnoAgentRuntime
from core.runtime.config import RuntimeConfig
from observe.traces import TraceStore
from test.fakes import FakeModel
from view.cli import InnoAgentCLI
from view.commands import command_info, parse_command
from view.render import render_event, render_events, render_sessions, render_state, render_tools
from view.terminal import TerminalIO
from view.tui_theme import OUTPUT_STYLE, TUI_STYLE


class CliTest(unittest.TestCase):
    """Tests for CLI command and rendering helpers."""

    def test_parse_slash_command(self) -> None:
        """Verify slash command parsing."""
        command = parse_command("/mode confirm")
        self.assertIsNotNone(command)
        self.assertEqual(command.name, "mode")
        self.assertEqual(command.args, ["confirm"])
        self.assertTrue(command_info("rename")["known"])
        self.assertTrue(command_info("steer")["known"])

    def test_slash_completion_filters_by_prefix_and_shows_description(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())
        completer = ui.session.completer
        self.assertIsNotNone(completer)

        matches = list(
            completer.get_completions(Document("/st", cursor_position=3), CompleteEvent())
        )
        self.assertEqual([item.text for item in matches], ["/status", "/steer", "/stop"])
        self.assertTrue(all(item.display_meta_text for item in matches))
        self.assertEqual(
            list(
                completer.get_completions(
                    Document("/model gpt", cursor_position=10),
                    CompleteEvent(),
                )
            ),
            [],
        )

    def test_non_tty_uses_plain_input_without_prompt_toolkit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgentRuntime(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            with (
                patch("view.cli.sys.stdin.isatty", return_value=False),
                patch("view.cli.TerminalIO") as terminal_io,
            ):
                cli = InnoAgentCLI(runtime)
            self.assertIsNone(cli.terminal)
            terminal_io.assert_not_called()

    def test_inline_tui_renders_streams_tools_and_status_toolbar(self) -> None:
        ui = TerminalIO(
            state_provider=lambda: {
                "session_id": "session-1",
                "model": "gpt-test",
                "mode": "ask",
                "max_context_tokens": 1000,
                "goal": "完善终端界面",
                "state": {
                    "context_usage": {
                        "used_tokens": 420,
                        "max_tokens": 1000,
                        "percent_used": 42,
                    },
                    "usage": {"total_tokens": 800},
                    "tasks": [
                        {"title": "实现布局", "status": "done"},
                        {"title": "补充测试", "status": "in_progress"},
                    ],
                },
            },
            app_input=DummyInput(),
            app_output=DummyOutput(),
        )
        ui.banner("gpt-test", "medium", "/tmp/InnoAgent", "ask")
        ui.add_user_message("实现新的 TUI")
        ui.handle_event({"type": "item.delta", "item_type": "message", "delta": "处理中"})
        ui.handle_event({"type": "response.completed"})
        ui.handle_event(
            {
                "type": "item.completed",
                "item_type": "tool_call",
                "tool_name": "read",
                "arguments": {"path": "README.md"},
            }
        )
        ui.handle_event(
            {
                "type": "item.completed",
                "item_type": "tool_result",
                "payload": {
                    "result": {
                        "tool_name": "read",
                        "status": "success",
                        "output": "读取完成",
                    }
                },
            }
        )
        self.assertIn("› You", ui.transcript_text)
        self.assertIn("• Agent", ui.transcript_text)
        self.assertIn("↳ Tool · read", ui.transcript_text)
        self.assertIn("✓ read · success", ui.transcript_text)
        toolbar = "".join(text for _, text in ui._bottom_toolbar())
        self.assertIn("ASK", toolbar)
        self.assertIn("42% context", toolbar)
        self.assertIn("Goal", toolbar)
        self.assertIn("Tasks 1/2", toolbar)
        self.assertNotIn("Ready", toolbar)
        self.assertIn(">_ InnoAgent  (v0.1.0)", ui.transcript_text)
        self.assertIn("model      gpt-test · medium", ui.transcript_text)
        self.assertIn("directory  /tmp/InnoAgent", ui.transcript_text)

    def test_inline_tui_keeps_native_scrollback_and_text_selection(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())

        self.assertFalse(ui.session.app.full_screen)
        self.assertFalse(ui.session.mouse_support)
        self.assertTrue(ui.session.app.erase_when_done)
        self.assertIsNotNone(ui.session.bottom_toolbar)
        self.assertEqual(ui.session.app.layout.current_window.style, "class:input")
        self.assertEqual(ui.session.app.layout.current_window.height.preferred, 2)

    def test_inline_tui_uses_local_blue_theme_without_global_background(self) -> None:
        rules = dict(TUI_STYLE.style_rules)

        self.assertNotIn("", rules)
        self.assertIn("#2563eb", rules["prompt"])
        self.assertIn("#60a5fa", rules["frame.border"])
        self.assertIn("bg:default", rules["bottom-toolbar"])
        self.assertFalse(
            any(
                "bg:#" in style
                for selector, style in TUI_STYLE.style_rules
                if selector.startswith("toolbar") or selector.startswith("bottom-toolbar")
            )
        )

    def test_streamed_text_survives_multiple_terminal_flushes(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())

        ui.handle_event({"type": "item.delta", "item_type": "message", "delta": "你好"})
        ui.handle_event({"type": "item.delta", "item_type": "message", "delta": "，我是"})
        ui.handle_event(
            {"type": "item.delta", "item_type": "message", "delta": " InnoAgent。\n下一行"}
        )
        ui.handle_event({"type": "response.completed"})

        self.assertIn("你好，我是 InnoAgent。", ui.transcript_text)
        self.assertIn("下一行", ui.transcript_text)
        self.assertEqual(ui._stream_pending, "")

    def test_agent_body_uses_explicit_black_text(self) -> None:
        attrs = OUTPUT_STYLE.get_attrs_for_style_str("class:output.body")

        self.assertEqual(attrs.color, "202124")
        self.assertFalse(attrs.bold)
        self.assertFalse(attrs.italic)

    def test_inline_tui_only_shows_meaningful_activity(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())

        idle_toolbar = "".join(text for _, text in ui._bottom_toolbar())
        self.assertNotIn("Ready", idle_toolbar)

        ui.set_busy(True, "Thinking")
        busy_toolbar = "".join(text for _, text in ui._bottom_toolbar())
        self.assertIn("Thinking", busy_toolbar)

    def test_tui_approval_changes_input_mode(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())
        ui.set_approval("write", "path=app.py")

        prompt = "".join(text for _, text in ui._input_prompt())
        self.assertEqual(prompt, "approve › ")
        self.assertIn("Approval · write", ui.transcript_text)
        self.assertIn("1 Allow once", ui.transcript_text)

    def test_tui_application_submits_input_and_exits(self) -> None:
        with create_pipe_input() as pipe_input:
            ui = TerminalIO(app_input=pipe_input, app_output=DummyOutput())
            submitted: list[str] = []

            def submit(text: str) -> None:
                submitted.append(text)
                ui.stop()

            pipe_input.send_text("/quit\n")
            ui.run(submit)

        self.assertEqual(submitted, ["/quit"])

    def test_ctrl_c_is_converted_to_graceful_quit(self) -> None:
        with create_pipe_input() as pipe_input:
            ui = TerminalIO(app_input=pipe_input, app_output=DummyOutput())
            submitted: list[str] = []

            def submit(text: str) -> None:
                submitted.append(text)
                ui.stop()

            pipe_input.send_bytes(b"\x03")
            ui.run(submit)

        self.assertEqual(submitted, ["/quit"])

    def test_quit_during_work_requests_stop_then_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgentRuntime(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=lambda _: None)
            cli.terminal = MagicMock()
            cli._tui_busy = True
            cli._tui_accepts_steering = True

            with patch.object(runtime, "request_stop", return_value=True) as request_stop:
                asyncio.run(cli._dispatch_tui_input("/quit"))

            self.assertTrue(cli._quit_when_idle)
            request_stop.assert_called_once_with(session_id=None)

    def test_inline_prompt_accepts_input_while_previous_work_is_running(self) -> None:
        with create_pipe_input() as pipe_input:
            ui = TerminalIO(app_input=pipe_input, app_output=DummyOutput())
            submitted: list[str] = []

            async def submit(text: str) -> None:
                submitted.append(text)
                if text == "first task":
                    ui.set_busy(True, "Thinking")
                    await asyncio.sleep(0.05)
                    ui.set_busy(False, "Ready")
                    return
                ui.stop()

            pipe_input.send_text("first task\nsteer now\n")
            ui.run(submit)

        self.assertEqual(submitted, ["first task", "steer now"])

    def test_busy_tui_input_is_routed_to_runtime_steering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgentRuntime(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            cli = InnoAgentCLI(
                runtime,
                input_fn=lambda prompt="": "",
                output_fn=lambda value: None,
            )
            cli.terminal = MagicMock()
            cli._tui_busy = True
            cli._tui_accepts_steering = True
            with patch.object(runtime, "submit_steering", return_value=True) as steering:
                asyncio.run(cli._dispatch_tui_input("先补测试"))

            cli.terminal.add_user_message.assert_called_once_with(
                "先补测试",
                steering=True,
            )
            steering.assert_called_once_with(
                "先补测试",
                session_id=None,
                delivery="after_tool",
            )

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

    def test_cli_can_clear_goal_for_existing_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                memory_enabled=False,
            )
            cli = InnoAgentCLI(
                InnoAgentRuntime(config, model=FakeModel()),
                input_fn=lambda prompt="": "",
                output_fn=lambda value: None,
            )
            cli._handle_command(parse_command("/goal create file"))
            cli._handle_task("创建 app.py")
            cli._handle_command(parse_command("/goal off"))
            cli._handle_task("读取 app.py")
            self.assertIsNone(cli.current_state.get("goal"))

    def test_cli_activates_skill_before_first_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / ".innoagent" / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo instructions\n---\nUse the demo workflow.\n",
                encoding="utf-8",
            )
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                memory_enabled=False,
            )
            cli = InnoAgentCLI(
                InnoAgentRuntime(config, model=FakeModel()),
                input_fn=lambda prompt="": "",
                output_fn=lambda value: None,
            )
            cli._handle_command(parse_command("/skill demo"))
            cli._handle_task("读取 readme.md")
            self.assertEqual(cli.current_state["active_skills"][0]["name"], "demo")


if __name__ == "__main__":
    unittest.main()
