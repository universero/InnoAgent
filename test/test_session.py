"""Session id, rename and JSONL replay tests."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from core.agent.react import InnoAgent
from core.runtime.config import RuntimeConfig
from core.session.store import SessionRecord, SessionStore
from core.tool.approval import ApprovalRequest
from test.fakes import FakeModel


class SessionTest(unittest.TestCase):
    """Tests for session identifiers and JSONL persistence."""

    def _runtime(self, tmp: str) -> InnoAgent:
        """Build a runtime with isolated storage."""
        config = RuntimeConfig(
            workspace_root=tmp,
            profile_root=str(Path(tmp) / "profiles"),
            session_root=str(Path(tmp) / "sessions"),
            memory_enabled=False,
        )
        return InnoAgent(config, model=FakeModel())

    def test_session_id_is_datetime_based(self) -> None:
        """Verify session ids use a datetime format."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            _, session_id = runtime.new_state("hello")
            self.assertRegex(session_id, r"^\d{8}-\d{6}-\d{6}$")

    def test_event_only_replay_aggregates_response_api_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            store.create(SessionRecord(session_id="usage-session"))
            store.append_events(
                "usage-session",
                [
                    {
                        "type": "response.completed",
                        "stage": "main",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 4,
                            "total_tokens": 14,
                            "cached_tokens": 3,
                            "reasoning_tokens": 2,
                        },
                    },
                    {
                        "type": "response.completed",
                        "stage": "reflect",
                        "usage": {
                            "input_tokens": 6,
                            "output_tokens": 2,
                            "total_tokens": 8,
                        },
                    },
                ],
            )

            state = store.load_state("usage-session")

            self.assertEqual(state["usage"]["input_tokens"], 16)
            self.assertEqual(state["usage"]["total_tokens"], 22)
            self.assertEqual(state["usage"]["requests"], 2)
            self.assertEqual(state["context_usage"]["used_tokens"], 10)
            self.assertEqual(state["context_usage"]["source"], "response_api")

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

    def test_event_only_replay_resets_evidence_when_goal_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            store.create(SessionRecord(session_id="goal-replay", goal="旧目标"))
            store.append_events(
                "goal-replay",
                [
                    {
                        "type": "item.completed",
                        "item_type": "plan",
                        "payload": {
                            "plan": {"status": "active"},
                            "tasks": [{"task_id": "old", "status": "done"}],
                        },
                    },
                    {
                        "type": "item.completed",
                        "item_type": "tool_result",
                        "tool_name": "write",
                        "payload": {
                            "result": {"tool_name": "write", "status": "success"}
                        },
                    },
                    {
                        "type": "item.completed",
                        "item_type": "reflection",
                        "payload": {"complete": True, "feedback": "旧结论"},
                    },
                    {
                        "type": "turn.started",
                        "payload": {
                            "user_input": "新目标",
                            "goal": "新目标",
                            "goal_restarted": True,
                        },
                    },
                ],
            )

            state = store.load_state("goal-replay")

            self.assertEqual(state["goal"], "新目标")
            self.assertIsNone(state["plan"])
            self.assertEqual(state["tasks"], [])
            self.assertIsNone(state["reflection"])
            self.assertFalse(state["goal_complete"])
            self.assertEqual(state["tool_results"], [])

    def test_event_only_replay_restores_usage_tasks_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            store.create(SessionRecord(session_id="state-replay"))
            plan = {"plan_id": "plan-1", "goal": "ship", "steps": []}
            tasks = [{"task_id": "task-1", "status": "in_progress"}]
            skill = {"name": "review", "content": "review carefully"}
            store.append_events(
                "state-replay",
                [
                    {
                        "type": "turn.started",
                        "payload": {"user_input": "ship", "goal": "ship"},
                    },
                    {
                        "type": "item.completed",
                        "item_type": "tool_result",
                        "tool_name": "task",
                        "payload": {
                            "result": {
                                "tool_name": "task",
                                "status": "success",
                                "data": {"plan": plan, "tasks": tasks},
                            }
                        },
                    },
                    {
                        "type": "item.completed",
                        "item_type": "tool_result",
                        "tool_name": "skill",
                        "payload": {
                            "result": {
                                "tool_name": "skill",
                                "status": "success",
                                "data": {"active_skill": skill},
                            }
                        },
                    },
                    {
                        "type": "turn.completed",
                        "payload": {
                            "finish_reason": "goal_complete",
                            "goal_complete": True,
                            "usage": {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
                            "context_usage": {"used_tokens": 100, "percent_used": 10.0},
                        },
                    },
                ],
            )

            state = store.load_state("state-replay")

            self.assertEqual(state["plan"], plan)
            self.assertEqual(state["tasks"], tasks)
            self.assertEqual(state["active_skills"], [skill])
            self.assertEqual(state["usage"]["total_tokens"], 11)
            self.assertEqual(state["context_usage"]["used_tokens"], 100)
            self.assertTrue(state["goal_complete"])

    def test_events_after_last_checkpoint_are_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            store.create(SessionRecord(session_id="checkpoint-tail"))
            store.append_events(
                "checkpoint-tail",
                [
                    {
                        "type": "state.checkpoint",
                        "state": {
                            "session_id": "checkpoint-tail",
                            "messages": [{"role": "user", "content": "before"}],
                            "goal": None,
                        },
                    },
                    {
                        "type": "turn.started",
                        "payload": {"user_input": "after", "goal": None},
                    },
                    {
                        "type": "item.completed",
                        "item_type": "message",
                        "payload": {"content": "persisted before crash"},
                    },
                    {
                        "type": "turn.failed",
                        "payload": {"error": "network disconnected"},
                    },
                ],
            )

            state = store.load_state("checkpoint-tail")

            self.assertEqual(state["messages"][-2]["content"], "after")
            self.assertEqual(state["messages"][-1]["content"], "persisted before crash")
            self.assertEqual(state["finish_reason"], "error")
            self.assertEqual(state["errors"][-1]["error"], "network disconnected")

    def test_legacy_approval_event_replays_as_resumable_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            store.create(SessionRecord(session_id="approval-replay"))
            store.append_events(
                "approval-replay",
                [
                    {
                        "type": "needs_confirmation",
                        "call_id": "legacy-write",
                        "tool_name": "write",
                        "arguments": {"path": "note.txt", "content": "hello"},
                        "output": "write requires approval",
                    }
                ],
            )

            state = store.load_state("approval-replay")

            self.assertEqual(state["pending_tool_calls"][0]["call_id"], "legacy-write")
            self.assertEqual(
                state["pending_confirmation"]["options"],
                ["allow_once", "allow_always", "deny"],
            )

    def test_approval_event_replays_deferred_tool_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            store.create(SessionRecord(session_id="approval-tail"))
            store.append_events(
                "approval-tail",
                [
                    {
                        "type": "approval.requested",
                        "payload": ApprovalRequest.from_calls(
                            [
                                {
                                    "call_id": "write-1",
                                    "name": "write",
                                    "arguments": {"path": "note.txt", "content": "new"},
                                }
                            ],
                            deferred_calls=[
                                {
                                    "call_id": "read-1",
                                    "name": "read",
                                    "arguments": {"path": "note.txt"},
                                }
                            ],
                        ).as_payload(),
                    }
                ],
            )

            state = store.load_state("approval-tail")

            self.assertEqual(state["pending_tool_calls"][0]["name"], "write")
            self.assertEqual(state["deferred_tool_calls"][0]["name"], "read")

    def test_runtime_can_resume_a_legacy_approval_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            session_id = "legacy-approval-runtime"
            runtime.session_store.create(SessionRecord(session_id=session_id, mode="ask"))
            runtime.session_store.append_events(
                session_id,
                [
                    {
                        "type": "needs_confirmation",
                        "call_id": "legacy-write",
                        "tool_name": "write",
                        "arguments": {"path": "note.txt", "content": "hello"},
                        "output": "write requires approval",
                    }
                ],
            )

            runtime.resolve_approval(session_id, "allow_once")

            self.assertEqual(Path(tmp, "note.txt").read_text(encoding="utf-8"), "hello")


if __name__ == "__main__":
    unittest.main()
