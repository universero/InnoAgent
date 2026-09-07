"""Session id, rename and JSONL replay tests."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from core.runtime.agent import InnoAgentRuntime
from core.runtime.config import RuntimeConfig
from core.session.store import SessionRecord, SessionStore
from test.fakes import FakeModel


class SessionTest(unittest.TestCase):
    """Tests for session identifiers and JSONL persistence."""

    def _runtime(self, tmp: str) -> InnoAgentRuntime:
        """Build a runtime with isolated storage."""
        config = RuntimeConfig(
            workspace_root=tmp,
            profile_root=str(Path(tmp) / "profiles"),
            session_root=str(Path(tmp) / "sessions"),
            memory_enabled=False,
        )
        return InnoAgentRuntime(config, model=FakeModel())

    def test_session_id_is_datetime_based(self) -> None:
        """Verify session ids use a datetime format."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            _, session_id = runtime.new_state("hello")
            self.assertRegex(session_id, r"^\d{8}-\d{6}-\d{6}$")

    def test_rename_is_preserved_after_continue(self) -> None:
        """Verify renamed sessions retain their name."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            Path(tmp, "readme.md").write_text("hello", encoding="utf-8")
            result = runtime.invoke("读取 readme.md")
            session_id = result["session_id"]
            record = runtime.rename_session(session_id, "我的阅读会话")
            self.assertEqual(record.name, "我的阅读会话")

            continued = runtime.invoke("读取 readme.md", session_id=session_id)
            loaded = runtime.session_store.load(continued["session_id"])
            self.assertEqual(loaded.name, "我的阅读会话")

    def test_session_can_be_resolved_by_unique_name_prefix(self) -> None:
        from view.resume import pick_session

        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            record = runtime.create_session()
            runtime.rename_session(record.session_id, "project-alpha")

            matched = pick_session(runtime, "project-a")

            self.assertIsNotNone(matched)
            self.assertEqual(matched.session_id, record.session_id)

    def test_session_is_jsonl_with_replayable_events(self) -> None:
        """Verify session files are line-delimited JSON events."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            Path(tmp, "readme.md").write_text("hello", encoding="utf-8")
            result = runtime.invoke("读取 readme.md")
            session_id = result["session_id"]
            path = Path(tmp) / "sessions" / f"{session_id}.jsonl"
            self.assertTrue(path.exists())
            events = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(json.loads(line))
            self.assertTrue(events)
            self.assertTrue(all(isinstance(event, dict) and "type" in event for event in events))
            self.assertEqual(events[0]["type"], "session_meta")
            self.assertTrue(
                any(
                    event["type"] == "item.completed"
                    and event.get("item_type") == "tool_result"
                    for event in events
                )
            )
            self.assertTrue(any(event["type"] == "turn.completed" for event in events))
            self.assertFalse(any(event["type"] == "item.delta" for event in events))

    def test_event_only_replay_restores_applied_steering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            store.create(SessionRecord(session_id="steering-replay"))
            store.append_events(
                "steering-replay",
                [
                    {
                        "type": "steering.applied",
                        "payload": {
                            "content": "改为只读分析",
                            "delivery": "after_tool",
                            "boundary": "after_tool",
                        },
                    }
                ],
            )

            state = store.load_state("steering-replay")
            self.assertEqual(
                state["steering_history"],
                [
                    {
                        "content": "改为只读分析",
                        "delivery": "after_tool",
                        "applied_at": "after_tool",
                    }
                ],
            )
            self.assertEqual(
                state["messages"][-1],
                {"role": "user", "content": "执行中用户纠偏：改为只读分析"},
            )

    def test_event_only_replay_restores_pending_user_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            store.create(SessionRecord(session_id="question-replay"))
            store.append_events(
                "question-replay",
                [
                    {
                        "type": "item.completed",
                        "item_type": "user_question",
                        "content": "Choose a target",
                        "options": ["web", "cli"],
                        "payload": {"allow_custom": True},
                    },
                    {
                        "type": "turn.completed",
                        "payload": {"finish_reason": "user_input_required"},
                    },
                ],
            )

            state = store.load_state("question-replay")

            self.assertEqual(
                state["pending_user_question"],
                {
                    "question": "Choose a target",
                    "options": ["web", "cli"],
                    "allow_custom": True,
                },
            )

    def test_event_only_replay_preserves_tool_call_relationship(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            store.create(SessionRecord(session_id="tool-replay"))
            store.append_events(
                "tool-replay",
                [
                    {
                        "type": "turn.started",
                        "payload": {"user_input": "read test.md"},
                    },
                    {
                        "type": "item.completed",
                        "item_type": "tool_call",
                        "call_id": "call_read",
                        "tool_name": "read",
                        "arguments": {"path": "test.md"},
                    },
                    {
                        "type": "item.completed",
                        "item_type": "tool_result",
                        "call_id": "call_read",
                        "tool_name": "read",
                        "payload": {
                            "result": {
                                "tool_name": "read",
                                "status": "success",
                                "output": "its a test",
                            }
                        },
                    },
                ],
            )

            messages = store.load_state("tool-replay")["messages"]

            self.assertEqual(messages[1]["tool_calls"][0]["call_id"], "call_read")
            self.assertEqual(messages[2]["tool_call_id"], "call_read")


if __name__ == "__main__":
    unittest.main()
