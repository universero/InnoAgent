"""CLI rendering and command tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.application.current import set_app
from prompt_toolkit.document import Document
from prompt_toolkit.completion import CompleteEvent

from core.agent.react import InnoAgent
from core.runtime.config import RuntimeConfig
from core.guardrails.policy import RunMode
from core.session.store import SessionRecord
from observe.traces import TraceStore
from test.fakes import FakeModel
from core.llm import BaseModelClient, ModelDecision, ToolCallDecision
from core.tool.approval import default_approval_options
from view.cli import InnoAgentCLI
from view.commands import CommandChoice, command_info, parse_command
from view.markdown import render_markdown
from view.render import render_event, render_events, render_sessions, render_state, render_tools
from view.resume import (
    recent_user_input,
    replayable_session_events,
    resume_summary,
    session_choice_description,
)
from view.terminal import CUSTOM_INPUT_OPTION, SelectionCompleter, SelectionState, TerminalIO
from view.tui_theme import build_output_style, build_tui_style, detect_color_scheme


_APPROVAL_OPTIONS = default_approval_options()
APPROVAL_VALUES = [option.value for option in _APPROVAL_OPTIONS]
APPROVAL_LABELS = {option.value: option.label for option in _APPROVAL_OPTIONS}


class CliTest(unittest.TestCase):
    """Tests for CLI command and rendering helpers."""

    class TimeoutAfterApprovalModel(BaseModelClient):
        """Request one shell call, then raise a model timeout."""

        def __init__(self) -> None:
            self.calls = 0

        def respond(
            self,
            context,
            tool_schemas,
            state=None,
            on_token=None,
            on_thinking=None,
        ):
            self.calls += 1
            if self.calls == 1:
                return ModelDecision(
                    action="tool_use",
                    tool_calls=[
                        ToolCallDecision(
                            name="shell",
                            arguments={"command": "echo approved-ok"},
                        )
                    ],
                )
            raise TimeoutError("simulated model timeout after approval")

    def test_parse_slash_command(self) -> None:
        """Verify slash command parsing."""
        command = parse_command("/mode readonly")
        self.assertIsNotNone(command)
        self.assertEqual(command.name, "mode")
        self.assertEqual(command.args, ["readonly"])
        self.assertTrue(command_info("rename")["known"])
        self.assertTrue(command_info("steer")["known"])

    def test_mode_command_rejects_unknown_modes(self) -> None:
        """Verify only ask, auto, and readonly are accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            outputs: list[str] = []
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            cli = InnoAgentCLI(runtime, output_fn=outputs.append)

            cli._handle_command(parse_command("/mode confirm"))
            self.assertEqual(runtime.config.mode, "ask")
            self.assertIn("valid modes: ask, auto, readonly", outputs)

            cli._handle_command(parse_command("/mode auto"))
            self.assertEqual(runtime.config.mode, "auto")

    def test_approval_failure_refreshes_cli_state(self) -> None:
        """A failed approval resolution must not leave stale pending state."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    mode=RunMode.ASK,
                    memory_enabled=False,
                ),
                model=self.TimeoutAfterApprovalModel(),
            )
            cli = InnoAgentCLI(runtime, output_fn=lambda _: None)
            result = runtime.invoke("run shell test")
            cli.current_session_id = result["session_id"]
            cli.current_state = result

            with self.assertRaises(TimeoutError):
                cli._resolve_approval("allow_once")

            self.assertIsNotNone(cli.current_state)
            self.assertFalse(cli.current_state.get("pending_confirmation"))
            self.assertEqual(cli.current_state.get("finish_reason"), "error")

    def test_context_command_updates_and_persists_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outputs: list[str] = []
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            cli = InnoAgentCLI(runtime, output_fn=outputs.append)

            cli._handle_command(parse_command("/context max 64k"))
            cli._handle_command(parse_command("/context threshold 75%"))
            cli._handle_command(parse_command("/context keep 8k"))

            self.assertEqual(runtime.config.max_context_tokens, 64_000)
            self.assertEqual(runtime.config.compact_threshold, 0.75)
            self.assertEqual(runtime.config.compact_threshold_tokens, 48_000)
            self.assertEqual(runtime.compactor.keep_recent_tokens, 8_000)
            config = yaml.safe_load(
                Path(tmp, ".innoagent", "config.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(config["max_context_tokens"], 64_000)
            self.assertIn("Responses API", outputs[-1])

    def test_compact_command_calls_runtime_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
                summarizer=lambda _: "summary",
            )
            state, session_id = runtime.new_state("seed")
            runtime.session_store.create(SessionRecord(session_id=session_id))
            state["messages"] = [
                {"role": "user", "content": "old " * 200},
                {"role": "assistant", "content": "result " * 200},
                {"role": "user", "content": "recent"},
            ]
            runtime._begin_run(session_id, "seed-turn")
            runtime._save_session(state)
            runtime._end_run()
            runtime.compactor.keep_recent_tokens = 10
            cli = InnoAgentCLI(runtime, output_fn=lambda _: None)
            cli.current_session_id = session_id

            cli._handle_command(parse_command("/compact preserve file decisions"))

            self.assertEqual(cli.current_state["context_summary"], "summary")
            event = next(
                item
                for item in runtime._run_events
                if item["type"] == "context.compaction.completed"
            )
            self.assertEqual(event["payload"]["trigger"], "manual")

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

    def test_slash_argument_completion_supports_static_and_dynamic_choices(self) -> None:
        def options(command_name: str) -> list[CommandChoice]:
            if command_name == "resume":
                return [
                    CommandChoice(
                        value="20260907-120000-000001",
                        display="project-alpha",
                        description="09-07 12:00 · fix tests",
                    )
                ]
            return []

        ui = TerminalIO(
            command_option_provider=options,
            app_input=DummyInput(),
            app_output=DummyOutput(),
        )
        completer = ui.session.completer
        self.assertIsNotNone(completer)

        mode = list(
            completer.get_completions(Document("/mode re"), CompleteEvent())
        )
        sessions = list(
            completer.get_completions(Document("/resume project"), CompleteEvent())
        )
        all_sessions = list(
            completer.get_completions(Document("/resume "), CompleteEvent())
        )

        self.assertEqual([item.text for item in mode], ["readonly"])
        self.assertEqual([item.text for item in sessions], ["20260907-120000-000001"])
        self.assertEqual(sessions[0].display_text, "project-alpha")
        self.assertEqual(len(all_sessions), 1)

    def test_tab_completes_a_dynamic_resume_candidate(self) -> None:
        with create_pipe_input() as pipe_input:
            ui = TerminalIO(
                command_option_provider=lambda name: [
                    CommandChoice("session-123", "project-alpha", "recent")
                ]
                if name == "resume"
                else [],
                app_input=pipe_input,
                app_output=DummyOutput(),
            )
            submitted: list[str] = []

            def submit(text: str) -> None:
                submitted.append(text)
                ui.stop()

            pipe_input.send_text("/resume pro\t\r")
            ui.run(submit)

        self.assertEqual(submitted, ["/resume session-123"])

    def test_slash_candidates_open_while_typing(self) -> None:
        async def inspect_completion_state() -> tuple[list[str], int]:
            with create_pipe_input() as pipe_input:
                ui = TerminalIO(
                    app_input=pipe_input,
                    app_output=DummyOutput(),
                )
                prompt = asyncio.create_task(
                    ui.session.prompt_async(
                        completer=ui._slash_completer,
                        complete_while_typing=True,
                    )
                )
                try:
                    await asyncio.sleep(0.05)
                    pipe_input.send_text("/")
                    for _ in range(20):
                        await asyncio.sleep(0.01)
                        state = ui.session.default_buffer.complete_state
                        if state is not None:
                            height = ui.session.app.layout.current_window.height()
                            return (
                                [item.text for item in state.completions],
                                height.preferred,
                            )
                    return [], 0
                finally:
                    prompt.cancel()
                    with suppress(asyncio.CancelledError):
                        await prompt

        candidates, input_height = asyncio.run(inspect_completion_state())

        self.assertIn("/help", candidates)
        self.assertIn("/resume", candidates)
        self.assertGreater(input_height, 2)

    def test_non_tty_uses_plain_input_without_prompt_toolkit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
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
        self.assertEqual(ui._input_height().preferred, 2)

    def test_tui_theme_detection_prefers_explicit_override(self) -> None:
        self.assertEqual(
            detect_color_scheme(
                environ={"INNOAGENT_THEME": "dark", "COLORFGBG": "0;15"},
                platform_name="Darwin",
                macos_appearance_reader=lambda: "Light",
            ),
            "dark",
        )
        self.assertEqual(
            detect_color_scheme(
                environ={"INNOAGENT_THEME": "light", "COLORFGBG": "15;0"},
                platform_name="Darwin",
                macos_appearance_reader=lambda: "Dark",
            ),
            "light",
        )

    def test_tui_theme_detection_uses_terminal_then_macos_appearance(self) -> None:
        self.assertEqual(
            detect_color_scheme(environ={"COLORFGBG": "15;0"}, platform_name="Linux"),
            "dark",
        )
        self.assertEqual(
            detect_color_scheme(environ={"COLORFGBG": "0;15"}, platform_name="Linux"),
            "light",
        )
        self.assertEqual(
            detect_color_scheme(
                environ={},
                platform_name="Darwin",
                macos_appearance_reader=lambda: "Dark",
            ),
            "dark",
        )
        self.assertEqual(
            detect_color_scheme(environ={}, platform_name="Linux"),
            "light",
        )

    def test_light_and_dark_tui_templates_keep_backgrounds_local(self) -> None:
        for scheme in ("light", "dark"):
            style = build_tui_style(scheme)
            rules = dict(style.style_rules)

            self.assertNotIn("", rules)
            self.assertIn("bg:default", rules["bottom-toolbar"])
            self.assertFalse(
                any(
                    "bg:#" in rule
                    for selector, rule in style.style_rules
                    if selector.startswith("toolbar")
                    or selector.startswith("bottom-toolbar")
                )
            )

        light_rules = dict(build_tui_style("light").style_rules)
        dark_rules = dict(build_tui_style("dark").style_rules)
        self.assertIn("bg:#eff6ff", light_rules["input"])
        self.assertIn("bg:#111827", dark_rules["input"])
        self.assertNotEqual(light_rules["input"], dark_rules["input"])

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

    def test_streamed_markdown_code_block_hides_fences(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())

        ui.handle_event(
            {
                "type": "item.delta",
                "item_type": "message",
                "delta": "已完成：\n```text\nits a test\n",
            }
        )
        ui.handle_event(
            {"type": "item.delta", "item_type": "message", "delta": "```"}
        )
        ui.handle_event({"type": "response.completed"})

        self.assertNotIn("```", ui.transcript_text)
        self.assertIn("╭─ text", ui.transcript_text)
        self.assertIn("│ its a test", ui.transcript_text)
        self.assertIn("╰─", ui.transcript_text)

    def test_reasoning_and_agent_text_use_distinct_blocks(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())

        ui.handle_event(
            {"type": "item.delta", "item_type": "reasoning", "delta": "先检查文件。\n"}
        )
        ui.handle_event(
            {"type": "item.delta", "item_type": "message", "delta": "检查完成。\n"}
        )
        ui.handle_event({"type": "response.completed"})

        self.assertIn("· Thinking", ui.transcript_text)
        self.assertIn("• Agent", ui.transcript_text)
        self.assertLess(ui.transcript_text.index("Thinking"), ui.transcript_text.index("Agent"))

    def test_completed_only_markdown_and_reasoning_are_formatted(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())

        ui.handle_event(
            {
                "type": "item.completed",
                "item_type": "reasoning",
                "payload": {"content": "先确认结果", "streamed": False},
            }
        )
        ui.handle_event(
            {
                "type": "item.completed",
                "item_type": "message",
                "payload": {
                    "content": "结果：\n```\nits a test\n```",
                    "streamed": False,
                },
            }
        )

        self.assertNotIn("```", ui.transcript_text)
        self.assertIn("· Thinking", ui.transcript_text)
        self.assertIn("• Agent", ui.transcript_text)
        self.assertIn("╭─ code", ui.transcript_text)
        self.assertIn("│ its a test", ui.transcript_text)

    def test_markdown_supports_inline_blocks_and_tables(self) -> None:
        fragments = render_markdown(
            "# Summary\n"
            "Use **strong**, *emphasis*, `inline()` and [docs](https://example.com).\n"
            "> quoted note\n"
            "- [x] completed\n"
            "1. ordered item\n"
            "---\n"
            "| Name | Status |\n"
            "| --- | :---: |\n"
            "| TUI | **Ready** |",
            base_style="class:output.body",
        )
        plain = "".join(text for _, text in fragments)
        styled = {(style, text) for style, text in fragments}

        self.assertNotIn("# Summary", plain)
        self.assertNotIn("**strong**", plain)
        self.assertNotIn("*emphasis*", plain)
        self.assertNotIn("`inline()`", plain)
        self.assertNotIn("| --- |", plain)
        self.assertIn("▸ Summary", plain)
        self.assertIn("strong", plain)
        self.assertIn("emphasis", plain)
        self.assertIn("inline()", plain)
        self.assertIn("docs (https://example.com)", plain)
        self.assertIn("│ quoted note", plain)
        self.assertIn("✓ completed", plain)
        self.assertIn("1. ordered item", plain)
        self.assertIn("┌", plain)
        self.assertIn("┼", plain)
        self.assertIn("┘", plain)
        self.assertTrue(
            any("output.strong" in style and text == "strong" for style, text in styled)
        )
        self.assertTrue(
            any(
                "output.emphasis" in style and text == "emphasis"
                for style, text in styled
            )
        )
        self.assertTrue(
            any(
                "output.inline-code" in style and text == "inline()"
                for style, text in styled
            )
        )

    def test_streamed_markdown_table_is_buffered_and_aligned(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())

        ui.handle_event(
            {
                "type": "item.delta",
                "item_type": "message",
                "delta": (
                    "结果如下：\n"
                    "| Name | Value |\n"
                    "| --- | ---: |\n"
                    "| file | `test.md` |\n"
                ),
            }
        )
        ui.handle_event({"type": "response.completed"})

        self.assertNotIn("| --- |", ui.transcript_text)
        self.assertNotIn("`test.md`", ui.transcript_text)
        self.assertIn("┌", ui.transcript_text)
        self.assertIn("│ file", ui.transcript_text)
        self.assertIn("test.md", ui.transcript_text)
        self.assertIn("┘", ui.transcript_text)

    def test_tui_hides_reflection_json_and_renders_semantic_result(self) -> None:
        ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())
        raw_json = '{"complete":true,"confidence":0.97,"summary":"verified"}'

        ui.handle_event(
            {
                "type": "item.delta",
                "stage": "reflect",
                "item_type": "message",
                "delta": raw_json + "\n",
            }
        )
        ui.handle_event(
            {
                "type": "item.completed",
                "stage": "reflect",
                "item_type": "message",
                "payload": {"content": raw_json, "streamed": False},
            }
        )
        ui.handle_event(
            {
                "type": "item.completed",
                "stage": "reflect",
                "item_type": "reasoning",
                "payload": {"content": "internal evaluator trace"},
            }
        )
        ui.handle_event(
            {
                "type": "item.completed",
                "stage": "reflect",
                "item_type": "reflection",
                "payload": {
                    "complete": True,
                    "confidence": 0.97,
                    "summary": "文件内容已经符合目标。",
                    "evidence": ["写入成功", "重新读取后内容正确"],
                },
            }
        )

        self.assertNotIn(raw_json, ui.transcript_text)
        self.assertNotIn("internal evaluator trace", ui.transcript_text)
        self.assertIn("Reflection · Goal complete", ui.transcript_text)
        self.assertIn("Confidence 97%", ui.transcript_text)
        self.assertIn("文件内容已经符合目标。", ui.transcript_text)
        self.assertIn("Evidence", ui.transcript_text)
        self.assertIn("• 写入成功", ui.transcript_text)

    def test_output_templates_use_readable_body_and_semantic_styles(self) -> None:
        required_styles = {
            "output.agent",
            "output.thinking",
            "output.body",
            "output.heading",
            "output.strong",
            "output.emphasis",
            "output.inline-code",
            "output.table.border",
            "output.code.border",
            "output.code",
            "startup.title",
            "startup.body",
        }
        light = build_output_style("light")
        dark = build_output_style("dark")

        self.assertTrue(required_styles.issubset(dict(light.style_rules)))
        self.assertTrue(required_styles.issubset(dict(dark.style_rules)))
        light_body = light.get_attrs_for_style_str("class:output.body")
        dark_body = dark.get_attrs_for_style_str("class:output.body")
        self.assertEqual(light_body.color, "202124")
        self.assertEqual(dark_body.color, "e5e7eb")
        self.assertFalse(light_body.bold)
        self.assertFalse(light_body.italic)
        self.assertFalse(dark_body.bold)
        self.assertFalse(dark_body.italic)

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
        self.assertNotIn("Allow once", ui.transcript_text)

    def test_approval_selector_uses_three_stable_rows(self) -> None:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        state = SelectionState(
            title="Approve",
            options=APPROVAL_VALUES,
            current=None,
            allow_custom=False,
            future=loop.create_future(),
            labels=APPROVAL_LABELS,
            selected_index=1,
        )

        completions = list(
            SelectionCompleter(state).get_completions(Document(""), CompleteEvent())
        )

        self.assertEqual([item.text for item in completions], APPROVAL_VALUES)
        self.assertEqual(
            [item.display_text for item in completions],
            ["Allow once", "Always allow in this workspace", "Deny"],
        )

    def test_approval_selector_uses_arrows_and_enter(self) -> None:
        with create_pipe_input() as pipe_input:
            ui = TerminalIO(app_input=pipe_input, app_output=DummyOutput())
            selected: list[str | None] = []

            async def submit(text: str) -> None:
                if text == "approve":
                    selected.append(
                        await ui.select(
                            "Approve",
                            APPROVAL_VALUES,
                            labels=APPROVAL_LABELS,
                        )
                    )
                    ui.stop()

            pipe_input.send_text("approve\n")
            pipe_input.send_bytes(b"\x1b[B\r")
            ui.run(submit)

        self.assertEqual(selected, ["allow_always"])

    def test_selector_navigation_updates_highlight_and_input_text(self) -> None:
        async def exercise() -> None:
            ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())
            state = SelectionState(
                title="Approve",
                options=APPROVAL_VALUES,
                current=None,
                allow_custom=False,
                future=asyncio.get_running_loop().create_future(),
                labels=APPROVAL_LABELS,
            )
            ui._selection = state
            buffer = ui.session.default_buffer
            event = SimpleNamespace(current_buffer=buffer, app=ui.session.app)

            with set_app(ui.session.app):
                ui._start_selection(state)
                self.assertEqual(buffer.text, "allow_once")
                self.assertEqual(buffer.complete_state.complete_index, 0)

                ui._move_selection(event, 1)

                self.assertEqual(buffer.text, "allow_always")
                self.assertEqual(buffer.complete_state.complete_index, 1)
                self.assertEqual(
                    buffer.complete_state.current_completion.display_text,
                    "Always allow in this workspace",
                )
                await asyncio.sleep(0)
                await ui.session.app.cancel_and_wait_for_background_tasks()

        asyncio.run(exercise())

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

    def test_inline_selector_uses_arrows_and_enter(self) -> None:
        with create_pipe_input() as pipe_input:
            ui = TerminalIO(app_input=pipe_input, app_output=DummyOutput())
            selected: list[str | None] = []

            async def submit(text: str) -> None:
                if text == "select":
                    selected.append(
                        await ui.select("model", ["alpha", "beta", "gamma"], current="beta")
                    )
                    ui.stop()

            pipe_input.send_text("select\n")
            pipe_input.send_bytes(b"\x1b[B\r")
            ui.run(submit)

        self.assertEqual(selected, ["gamma"])

    def test_inline_selector_custom_choice_accepts_text(self) -> None:
        with create_pipe_input() as pipe_input:
            ui = TerminalIO(app_input=pipe_input, app_output=DummyOutput())
            selected: list[str | None] = []

            async def submit(text: str) -> None:
                if text == "question":
                    selected.append(
                        await ui.select("answer", ["yes", "no"], allow_custom=True)
                    )
                    ui.stop()

            pipe_input.send_text("question\n")
            pipe_input.send_bytes(b"\x1b[B\x1b[B\rcustom answer\r")
            ui.run(submit)

        self.assertEqual(selected, ["custom answer"])

    def test_ctrl_c_during_selection_still_quits_gracefully(self) -> None:
        with create_pipe_input() as pipe_input:
            ui = TerminalIO(app_input=pipe_input, app_output=DummyOutput())
            submitted: list[str] = []
            selected: list[str | None] = []

            async def submit(text: str) -> None:
                submitted.append(text)
                if text == "select":
                    selected.append(await ui.select("model", ["alpha", "beta"]))
                elif text == "/quit":
                    ui.stop()

            pipe_input.send_text("select\n")
            pipe_input.send_bytes(b"\x03")
            ui.run(submit)

        self.assertEqual(selected, [None])
        self.assertEqual(submitted, ["select", "/quit"])

    def test_selector_marks_current_value_and_adds_custom_option(self) -> None:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        state = SelectionState(
            title="model",
            options=["alpha", "beta"],
            current="beta",
            allow_custom=True,
            future=loop.create_future(),
            selected_index=1,
        )
        completions = list(
            SelectionCompleter(state).get_completions(Document(""), CompleteEvent())
        )

        self.assertEqual(
            [item.text for item in completions],
            ["alpha", "beta", CUSTOM_INPUT_OPTION],
        )
        self.assertIn("current", completions[1].display_text)

    def test_pending_approval_uses_selector_without_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
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
            cli.terminal.select = AsyncMock(return_value="allow_always")
            cli.current_session_id = "session-1"
            cli.current_state = {
                "pending_confirmation": {
                    "tool_name": "write",
                    "arguments": {"path": "app.py"},
                }
            }

            async def run_work(function, activity, **kwargs) -> None:
                function()

            with (
                patch.object(
                    runtime,
                    "resolve_approval",
                    return_value={"pending_confirmation": None},
                ) as resolve,
                patch.object(cli, "_run_tui_work", side_effect=run_work),
            ):
                asyncio.run(cli._resolve_pending_interactions())

            cli.terminal.select.assert_awaited_once()
            selection = cli.terminal.select.await_args
            self.assertEqual(selection.args, ("Approve", APPROVAL_VALUES))
            self.assertEqual(selection.kwargs["labels"], APPROVAL_LABELS)
            resolve.assert_called_once_with("session-1", "allow_always")
            cli.terminal.add_user_message.assert_not_called()

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
            runtime = InnoAgent(
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
            runtime = InnoAgent(
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

    def test_model_command_fetches_provider_models_and_uses_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from core.llm import OpenAICompatibleModel

            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=OpenAICompatibleModel("key", "https://example.com/v1", "alpha"),
            )
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=lambda _: None)
            cli.terminal = MagicMock()
            cli.terminal.select = AsyncMock(side_effect=["beta", "high"])

            with patch.object(runtime, "list_models", return_value=["alpha", "beta"]):
                asyncio.run(cli._dispatch_tui_input("/model"))

            self.assertEqual(runtime.model.model, "beta")
            self.assertEqual(runtime.model.reasoning_effort, "high")
            self.assertEqual(cli.terminal.select.await_count, 2)
            cli.terminal.select.assert_any_await(
                "Select model",
                ["alpha", "beta"],
                current="alpha",
            )
            effort_selection = cli.terminal.select.await_args_list[1]
            self.assertEqual(effort_selection.args[0], "Select reasoning effort")
            self.assertEqual(effort_selection.args[1], ["none", "low", "medium", "high"])
            self.assertEqual(effort_selection.kwargs["current"], "none")
            self.assertEqual(effort_selection.kwargs["labels"]["none"], "No reasoning")
            self.assertIn("balanced", effort_selection.kwargs["descriptions"]["medium"])
            cli.terminal.set_model.assert_called_once_with("beta", "high")
            cli.terminal.add_user_message.assert_not_called()

    def test_consecutive_selector_values_do_not_reenter_input_dispatch(self) -> None:
        with create_pipe_input() as pipe_input:
            terminal = TerminalIO(app_input=pipe_input, app_output=DummyOutput())
            submitted: list[str] = []
            selections: list[str | None] = []

            async def submit(text: str) -> None:
                submitted.append(text)
                if text != "/model":
                    return
                selections.append(
                    await terminal.select("Select model", ["alpha", "beta"], current="alpha")
                )
                selections.append(
                    await terminal.select(
                        "Select reasoning effort",
                        ["none", "low", "medium", "high"],
                        current="none",
                    )
                )
                terminal.stop()

            pipe_input.send_text("/model\n")
            pipe_input.send_bytes(b"\x1b[B\r\x1b[B\x1b[B\r")
            terminal.run(submit)

            self.assertEqual(submitted, ["/model"])
            self.assertEqual(selections, ["beta", "medium"])

    def test_model_command_cancels_without_partial_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from core.llm import OpenAICompatibleModel

            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=OpenAICompatibleModel("key", "https://example.com/v1", "alpha"),
            )
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=lambda _: None)
            cli.terminal = MagicMock()
            cli.terminal.select = AsyncMock(side_effect=["beta", None])

            with patch.object(runtime, "list_models", return_value=["alpha", "beta"]):
                asyncio.run(cli._dispatch_tui_input("/model"))

            self.assertEqual(runtime.model.model, "alpha")
            self.assertEqual(runtime.model.reasoning_effort, "none")
            cli.terminal.set_model.assert_not_called()

    def test_model_command_can_change_effort_without_changing_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from core.llm import OpenAICompatibleModel

            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=OpenAICompatibleModel("key", "https://example.com/v1", "alpha"),
            )
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=lambda _: None)
            cli.terminal = MagicMock()
            cli.terminal.select = AsyncMock(side_effect=["alpha", "medium"])

            with patch.object(runtime, "list_models", return_value=["alpha", "beta"]):
                asyncio.run(cli._dispatch_tui_input("/model"))

            self.assertEqual(runtime.model.model, "alpha")
            self.assertEqual(runtime.model.reasoning_effort, "medium")
            cli.terminal.set_model.assert_called_once_with("alpha", "medium")

    def test_pending_user_question_rejects_model_control_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            output: list[str] = []
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=output.append)
            cli.terminal = MagicMock()
            cli.terminal.select = AsyncMock(return_value=None)
            cli.current_state = {
                "pending_user_question": {
                    "question": "Choose one",
                    "options": ["yes", "no"],
                    "allow_custom": False,
                }
            }

            with patch.object(runtime, "list_models") as list_models:
                asyncio.run(cli._dispatch_tui_input("/model"))

            list_models.assert_not_called()
            cli.terminal.select.assert_awaited_once_with(
                "Answer",
                ["yes", "no"],
                allow_custom=False,
            )
            cli.terminal.add_user_message.assert_not_called()
            self.assertTrue(any("A user question is pending" in line for line in output))

    def test_resume_command_uses_session_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            record = runtime.create_session()
            runtime.session_store.append_events(
                record.session_id,
                [
                    {
                        "type": "turn.started",
                        "payload": {"user_input": "继续完善会话恢复体验"},
                    }
                ],
            )
            runtime.rename_session(record.session_id, "project-alpha")
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=lambda _: None)
            cli.terminal = MagicMock()
            cli.terminal.select = AsyncMock(return_value=record.session_id)

            asyncio.run(cli._dispatch_tui_input("/resume"))

            self.assertEqual(cli.current_session_id, record.session_id)
            cli.terminal.select.assert_awaited_once()
            call = cli.terminal.select.await_args
            self.assertEqual(call.args[0], "Resume session")
            self.assertEqual(call.args[1], [record.session_id])
            self.assertEqual(call.kwargs["labels"][record.session_id], "project-alpha")
            description = call.kwargs["descriptions"][record.session_id]
            self.assertIn("继续完善会话恢复体验", description)
            self.assertNotIn(record.session_id, description)
            self.assertNotIn("goal", description.lower())

    def test_session_metadata_uses_truncated_recent_user_input(self) -> None:
        record = SessionRecord(
            session_id="session-123",
            goal="不应展示的目标",
            state={
                "user_input": (
                    "  这是一个很长的用户输入，\n"
                    "用于验证候选项只展示最近输入而不是目标  "
                ),
            },
        )

        preview = recent_user_input(record, limit=18)
        description = session_choice_description(record)
        summary = resume_summary(record)

        self.assertEqual(len(preview), 18)
        self.assertTrue(preview.endswith("…"))
        self.assertIn("这是一个很长的用户输入", description)
        self.assertNotIn(record.session_id, description)
        self.assertNotIn("不应展示的目标", description)
        self.assertIn("最近输入", summary)
        self.assertNotIn("goal", summary.lower())
        self.assertNotIn("不应展示的目标", summary)
        sessions = render_sessions([record])
        self.assertEqual(sessions.count(record.session_id), 1)
        self.assertIn("最近输入=", sessions)
        self.assertNotIn("不应展示的目标", sessions)

    def test_replayable_session_events_keep_recent_complete_turns(self) -> None:
        events = [
            {"type": "session_meta"},
            {"type": "turn.started", "payload": {"user_input": "first"}},
            {
                "type": "item.completed",
                "item_type": "message",
                "payload": {"content": "first answer", "streamed": True},
            },
            {"type": "response.completed"},
            {"type": "state.checkpoint", "state": {}},
            {"type": "turn.started", "payload": {"user_input": "second"}},
            {
                "type": "item.completed",
                "item_type": "message",
                "payload": {"content": "second answer", "streamed": True},
            },
            {"type": "approval.resolved", "payload": {"decision": "allow_once"}},
        ]

        replay = replayable_session_events(events, max_turns=1, max_events=10)

        self.assertEqual(
            [event["type"] for event in replay],
            ["turn.started", "item.completed", "approval.resolved"],
        )
        self.assertEqual(replay[0]["payload"]["user_input"], "second")
        self.assertFalse(replay[1]["payload"]["streamed"])
        self.assertTrue(events[6]["payload"]["streamed"])

    def test_tui_resume_replays_persisted_transcript_without_state_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            record = runtime.create_session()
            runtime.session_store.append_events(
                record.session_id,
                [
                    {
                        "type": "turn.started",
                        "payload": {"user_input": "读取历史文件"},
                    },
                    {
                        "type": "item.completed",
                        "item_type": "message",
                        "payload": {"content": "我先检查文件。", "streamed": True},
                    },
                    {
                        "type": "item.completed",
                        "item_type": "tool_call",
                        "tool_name": "read",
                        "arguments": {"path": "history.md"},
                    },
                    {
                        "type": "item.completed",
                        "item_type": "tool_result",
                        "tool_name": "read",
                        "payload": {
                            "result": {
                                "tool_name": "read",
                                "status": "success",
                                "output": "历史文件内容",
                            }
                        },
                    },
                    {
                        "type": "item.completed",
                        "item_type": "message",
                        "payload": {"content": "历史答复已完成。", "streamed": True},
                    },
                    {"type": "turn.completed", "payload": {}},
                ],
            )
            ui = TerminalIO(app_input=DummyInput(), app_output=DummyOutput())
            cli = InnoAgentCLI(
                runtime,
                input_fn=lambda prompt="": "",
                output_fn=lambda _: None,
            )
            cli.terminal = ui
            cli.output_fn = ui.output

            cli._resume_session(record.session_id)

            self.assertIn("› You", ui.transcript_text)
            self.assertIn("读取历史文件", ui.transcript_text)
            self.assertIn("• Agent", ui.transcript_text)
            self.assertIn("我先检查文件。", ui.transcript_text)
            self.assertIn("↳ Tool · read", ui.transcript_text)
            self.assertIn("✓ read · success", ui.transcript_text)
            self.assertEqual(ui.transcript_text.count("历史答复已完成。"), 1)
            self.assertIn("最近输入：读取历史文件", ui.transcript_text)

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
        """Verify /goal starts a turn before rename and resume."""
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                memory_enabled=False,
            )
            runtime = InnoAgent(config, model=FakeModel())
            output: list[str] = []
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=output.append)
            cli._handle_command(parse_command("/goal create file"))
            self.assertEqual(cli.current_goal, "create file")
            self.assertIsNotNone(cli.current_session_id)
            cli._handle_command(parse_command("/rename 项目初始化"))
            self.assertTrue(any("已重命名为" in line for line in output))
            cli._handle_command(parse_command("/resume"))
            self.assertTrue(any("已恢复" in line for line in output))

    def test_goal_command_passes_goal_as_user_input_and_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=lambda _: None)
            result = {
                "session_id": "session-1",
                "goal": "在test.md中追加333",
                "active_skills": [],
            }

            with patch.object(runtime, "invoke", return_value=result) as invoke:
                cli._handle_command(parse_command("/goal 在test.md中追加333"))

            invoke.assert_called_once_with(
                "在test.md中追加333",
                session_id=None,
                goal="在test.md中追加333",
                active_skills=[],
                restart_goal=True,
            )
            self.assertEqual(cli.current_session_id, "session-1")
            self.assertEqual(cli.current_state, result)

    def test_tui_goal_command_runs_as_steerable_agent_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
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
            work: list[tuple[str, bool]] = []

            async def run_work(function, activity, *, accepts_steering=False) -> None:
                work.append((activity, accepts_steering))
                function()

            with (
                patch.object(cli, "_run_tui_work", side_effect=run_work),
                patch.object(cli, "_handle_command") as handle_command,
                patch.object(
                    cli,
                    "_resolve_pending_interactions",
                    new=AsyncMock(),
                ) as resolve_pending,
            ):
                asyncio.run(cli._dispatch_tui_input("/goal 在test.md中追加333"))

            self.assertEqual(work, [("Thinking", True)])
            handle_command.assert_called_once()
            self.assertFalse(handle_command.call_args.kwargs["request_approval"])
            resolve_pending.assert_awaited_once()

    def test_rename_creates_an_empty_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            output: list[str] = []
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=output.append)

            cli._handle_command(parse_command("/rename test-session"))

            self.assertIsNotNone(cli.current_session_id)
            record = runtime.session_store.load(str(cli.current_session_id))
            self.assertEqual(record.name, "test-session")
            self.assertTrue(any("test-session" in line for line in output))

            cli._handle_task("hello")
            continued = runtime.session_store.load(str(cli.current_session_id))
            self.assertEqual(continued.name, "test-session")

    def test_goal_without_argument_reports_without_clearing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    memory_enabled=False,
                ),
                model=FakeModel(),
            )
            output: list[str] = []
            cli = InnoAgentCLI(runtime, input_fn=lambda prompt="": "", output_fn=output.append)
            cli.current_goal = "keep this goal"

            cli._handle_command(parse_command("/goal"))

            self.assertEqual(cli.current_goal, "keep this goal")
            self.assertEqual(output[-1], "goal: keep this goal")

    def test_cli_can_clear_goal_for_existing_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                memory_enabled=False,
            )
            cli = InnoAgentCLI(
                InnoAgent(config, model=FakeModel()),
                input_fn=lambda prompt="": "",
                output_fn=lambda value: None,
            )
            cli._handle_command(parse_command("/goal create file"))
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
                InnoAgent(config, model=FakeModel()),
                input_fn=lambda prompt="": "",
                output_fn=lambda value: None,
            )
            cli._handle_command(parse_command("/skill demo"))
            cli._handle_task("读取 readme.md")
            self.assertEqual(cli.current_state["active_skills"][0]["name"], "demo")


if __name__ == "__main__":
    unittest.main()
