"""End-to-end main loop tests using a deterministic fake model."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.guardrails.policy import RunMode
from core.runtime.agent import InnoAgentRuntime
from core.runtime.config import RuntimeConfig
from core.llm import BaseModelClient, ModelDecision, ToolCallDecision
from test.fakes import FakeModel


class MainLoopTest(unittest.TestCase):
    """Tests for the event-driven agent loop."""

    class RepeatReadModel(BaseModelClient):
        """Fake model that repeatedly asks to read test.md."""

        def respond(self, context, tool_schemas, state=None, on_token=None, on_thinking=None):
            """Always request a read of test.md."""
            return ModelDecision(
                action="tool_use",
                tool_calls=[ToolCallDecision(name="read", arguments={"path": "test.md"})],
            )

    class StreamTextModel(BaseModelClient):
        def respond(self, context, tool_schemas, state=None, on_token=None, on_thinking=None):
            if on_token:
                on_token("hello")
                on_token(" world")
            return ModelDecision(action="finish", message="hello world")

    class WriteThenReadModel(BaseModelClient):
        """Issue a dependent write/read batch to exercise approval ordering."""

        def respond(self, context, tool_schemas, state=None, on_token=None, on_thinking=None):
            if not (state or {}).get("tool_results"):
                return ModelDecision(
                    action="tool_use",
                    tool_calls=[
                        ToolCallDecision(
                            name="write",
                            arguments={"path": "value.txt", "content": "new"},
                        ),
                        ToolCallDecision(name="read", arguments={"path": "value.txt"}),
                    ],
                )
            return ModelDecision(action="finish", message="completed")

    def test_goal_creates_and_verifies_file(self) -> None:
        """Verify a goal can create and verify a file."""
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                mode="auto",
                max_iterations=20,
                memory_enabled=False,
            )
            runtime = InnoAgentRuntime(config, model=FakeModel())
            goal = "创建 app.py 并打印 Hello, InnoAgent"
            result = runtime.invoke(goal, goal=goal)
            self.assertTrue(result.get("goal_complete"))
            app_path = Path(tmp) / "app.py"
            self.assertTrue(app_path.exists())
            self.assertIn("Hello, InnoAgent", app_path.read_text(encoding="utf-8"))

    def test_restarting_goal_clears_previous_goal_evidence(self) -> None:
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
            record = runtime.create_session(goal="旧目标")
            state = runtime.session_store.load_state(record.session_id)
            state.update(
                plan={"status": "active", "steps": [{"title": "旧步骤"}]},
                tasks=[{"task_id": "old", "title": "旧任务", "status": "done"}],
                reflection={"complete": True, "feedback": "旧结论"},
                reflection_count=2,
                goal_complete=True,
                tool_results=[{"tool_name": "write", "status": "success"}],
                errors=[{"message": "旧错误"}],
            )
            runtime._save_session(state)

            with patch.object(runtime, "_run_react", side_effect=lambda current: current):
                result = runtime.invoke(
                    "新目标",
                    session_id=record.session_id,
                    goal="新目标",
                    restart_goal=True,
                )

            self.assertEqual(result["goal"], "新目标")
            self.assertFalse(result["goal_complete"])
            self.assertIsNone(result["plan"])
            self.assertEqual(result["tasks"], [])
            self.assertIsNone(result["reflection"])
            self.assertEqual(result["reflection_count"], 0)
            self.assertEqual(result["tool_results"], [])
            self.assertEqual(result["errors"], [])

    def test_new_turn_resets_transient_goal_evidence_but_keeps_plan(self) -> None:
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
            record = runtime.create_session(goal="持续目标")
            state = runtime.session_store.load_state(record.session_id)
            plan = {"plan_id": "stable", "status": "active", "steps": []}
            state.update(
                plan=plan,
                tasks=[{"task_id": "next", "status": "pending"}],
                reflection={"complete": False},
                reflection_count=2,
                tool_results=[{"tool_name": "read", "status": "error"}],
                errors=[{"message": "旧错误"}],
            )
            runtime._save_session(state)

            with patch.object(runtime, "_run_react", side_effect=lambda current: current):
                result = runtime.invoke(
                    "继续",
                    session_id=record.session_id,
                    goal="持续目标",
                )

            self.assertEqual(result["plan"], plan)
            self.assertEqual(result["tasks"], [{"task_id": "next", "status": "pending"}])
            self.assertEqual(result["reflection_count"], 0)
            self.assertIsNone(result["reflection"])
            self.assertEqual(result["tool_results"], [])
            self.assertEqual(result["errors"], [])

    def test_no_goal_reads_file(self) -> None:
        """Verify a simple read request without a goal."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "readme.md").write_text("hello from temp", encoding="utf-8")
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                memory_enabled=False,
            )
            runtime = InnoAgentRuntime(config, model=FakeModel())
            result = runtime.invoke("读取 readme.md")
            self.assertTrue(result.get("finished"))
            self.assertIn("hello from temp", result.get("response", ""))
            assistant_call = next(
                message
                for message in result["messages"]
                if message.get("tool_calls")
            )
            tool_message = next(
                message
                for message in result["messages"]
                if message.get("role") == "tool"
            )
            self.assertEqual(
                assistant_call["tool_calls"][0]["call_id"],
                tool_message["tool_call_id"],
            )

    def test_ask_mode_write_requires_approval_then_executes(self) -> None:
        """Verify ask mode blocks writes until approval."""
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                mode=RunMode.ASK,
                memory_enabled=False,
            )
            runtime = InnoAgentRuntime(config, model=FakeModel())
            result = runtime.invoke("写入 note.txt 内容 hello")
            self.assertEqual(result.get("pending_confirmation", {}).get("tool_name"), "write")
            self.assertFalse(
                any(
                    item.get("status") == "needs_confirmation"
                    for item in result.get("tool_results", [])
                )
            )
            session_id = result["session_id"]
            approved = runtime.approve_pending(session_id)
            self.assertTrue((Path(tmp) / "note.txt").exists())
            self.assertTrue(approved.get("finished"))

    def test_pending_write_blocks_later_read_until_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "value.txt"
            path.write_text("old", encoding="utf-8")
            runtime = InnoAgentRuntime(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=str(Path(tmp) / "profiles"),
                    session_root=str(Path(tmp) / "sessions"),
                    mode="ask",
                    memory_enabled=False,
                ),
                model=self.WriteThenReadModel(),
            )

            pending = runtime.invoke("replace and verify")

            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(pending["pending_tool_calls"][0]["name"], "write")
            self.assertEqual(pending["deferred_tool_calls"][0]["name"], "read")
            self.assertEqual(pending["tool_results"], [])

            approved = runtime.resolve_approval(pending["session_id"], "allow_once")

            self.assertEqual(path.read_text(encoding="utf-8"), "new")
            self.assertEqual(
                [result["tool_name"] for result in approved["tool_results"]],
                ["write", "read"],
            )
            self.assertEqual(approved["tool_results"][1]["output"], "new")

    def test_repeated_identical_tool_call_stops_and_returns_result(self) -> None:
        """Verify repeated identical tool calls stop the loop."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test.md").write_text("its a test", encoding="utf-8")
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                memory_enabled=False,
            )
            runtime = InnoAgentRuntime(config, model=self.RepeatReadModel())
            result = runtime.invoke("读test.md")
            self.assertTrue(result.get("finished"))
            self.assertEqual(result.get("response"), "its a test")
            self.assertEqual(len(result.get("tool_results", [])), 1)
            final_messages = [
                event
                for event in runtime._run_events
                if event["type"] == "item.completed"
                and event.get("item_type") == "message"
                and event.get("content") == "its a test"
            ]
            self.assertEqual(len(final_messages), 1)

    def test_model_response_emits_text_delta_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                memory_enabled=False,
            )
            runtime = InnoAgentRuntime(config, model=self.StreamTextModel())
            runtime.invoke("hello")
            text_events = [
                event
                for event in runtime._run_events
                if event["type"] == "item.delta" and event.get("item_type") == "message"
            ]
            self.assertEqual([event["delta"] for event in text_events], ["hello", " world"])


if __name__ == "__main__":
    unittest.main()
