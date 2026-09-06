"""End-to-end main loop tests using a deterministic fake model."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.runtime.agent import InnoAgentRuntime
from core.runtime.config import RuntimeConfig
from core.llm import BaseModelClient, ModelDecision, ToolCallDecision
from test.fakes import FakeModel


class MainLoopTest(unittest.TestCase):
    """Tests for the compiled LangGraph main loop."""

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

    def test_confirm_mode_write_requires_approval_then_executes(self) -> None:
        """Verify confirm mode blocks writes until approval."""
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                mode="confirm",
                memory_enabled=False,
            )
            runtime = InnoAgentRuntime(config, model=FakeModel())
            result = runtime.invoke("写入 note.txt 内容 hello")
            self.assertEqual(result.get("pending_confirmation", {}).get("tool_name"), "write")
            session_id = result["session_id"]
            approved = runtime.approve_pending(session_id)
            self.assertTrue((Path(tmp) / "note.txt").exists())
            self.assertTrue(approved.get("finished"))

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
            text_events = [event for event in runtime._run_events if event["type"] == "text.delta"]
            self.assertEqual([event["content"] for event in text_events], ["hello", " world"])


if __name__ == "__main__":
    unittest.main()
