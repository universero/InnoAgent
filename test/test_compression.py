"""Structured context compaction tests."""

from __future__ import annotations

import unittest

from core.memory.profile import UserProfile
from core.session.compression import ContextCompactor
from core.session.context import ContextBuilder
from core.session.history import Message, SessionHistory


class CompressionTest(unittest.TestCase):
    def test_compaction_keeps_recent_messages_and_reports_ratio(self) -> None:
        messages = [Message(role="user", content=f"message {index} " * 20) for index in range(8)]
        compactor = ContextCompactor(keep_recent_tokens=30)
        result = compactor.compact(messages, lambda prompt: "structured summary")
        self.assertEqual(result.messages[0].role, "summary")
        self.assertEqual(result.summary, "structured summary")
        self.assertLess(result.tokens_after, result.tokens_before)
        self.assertLess(result.compression_ratio, 1.0)

    def test_compaction_falls_back_when_summarizer_fails(self) -> None:
        messages = [
            Message(role="user", content="x" * 100),
            Message(role="assistant", content="y" * 100),
            Message(role="user", content="recent request"),
            Message(role="assistant", content="recent response"),
        ]
        compactor = ContextCompactor(keep_recent_tokens=10)

        def fail(_: str) -> str:
            raise RuntimeError("offline")

        result = compactor.compact(messages, fail)
        self.assertIn("Goal and user intent", result.summary)

    def test_context_does_not_repeat_persisted_summary(self) -> None:
        history = SessionHistory(
            [
                Message(role="summary", content="structured summary"),
                Message(role="user", content="recent request"),
            ]
        )
        context = ContextBuilder(max_tokens=1000).build(
            "continue",
            history,
            UserProfile.default_for("default"),
            [],
            context_summary="structured summary",
        )
        self.assertEqual(context.count("structured summary"), 1)
        self.assertIn("recent request", context)

    def test_context_does_not_duplicate_current_user_message(self) -> None:
        history = SessionHistory([Message(role="user", content="read test.md")])
        builder = ContextBuilder(max_tokens=1000)

        context = builder.build(
            "read test.md",
            history,
            UserProfile.default_for("default"),
            [],
        )

        self.assertEqual(context.count("read test.md"), 1)
        self.assertNotIn("read test.md", builder.last_runtime_context)

    def test_context_always_exposes_active_goal_to_model(self) -> None:
        builder = ContextBuilder(max_tokens=1000)

        context = builder.build(
            "继续执行",
            SessionHistory([Message(role="user", content="继续执行")]),
            UserProfile.default_for("default"),
            [],
            goal="在 test.md 中追加 333",
        )

        self.assertIn("当前目标：\n在 test.md 中追加 333", context)
        self.assertIn("当前目标：\n在 test.md 中追加 333", builder.last_runtime_context)

    def test_compaction_keeps_complete_recent_user_turn(self) -> None:
        messages = [
            Message(role="user", content="old request " * 20),
            Message(role="assistant", content="old response " * 20),
            Message(role="user", content="recent request " * 8),
            Message(role="assistant", content="recent response"),
        ]
        result = ContextCompactor(keep_recent_tokens=8).compact(
            messages,
            lambda prompt: "older turn summary",
        )
        self.assertEqual(
            [message.role for message in result.messages],
            ["summary", "user", "assistant"],
        )

    def test_compaction_preserves_structured_tool_call_relationship(self) -> None:
        messages = [
            Message(role="user", content="old " * 100),
            Message(role="assistant", content="old result " * 100),
            Message(role="user", content="read test.md"),
            Message(
                role="assistant",
                tool_calls=[
                    {
                        "call_id": "call_read",
                        "name": "read",
                        "arguments": {"path": "test.md"},
                    }
                ],
            ),
            Message(
                role="tool",
                content="its a test",
                tool_call_id="call_read",
                name="read",
            ),
        ]

        result = ContextCompactor(keep_recent_tokens=30).compact(
            messages,
            lambda _: "older summary",
        )
        serialized = [message.as_dict() for message in result.messages]

        assistant = next(item for item in serialized if item.get("tool_calls"))
        tool = next(item for item in serialized if item.get("role") == "tool")
        self.assertEqual(assistant["tool_calls"][0]["call_id"], "call_read")
        self.assertEqual(tool["tool_call_id"], "call_read")

    def test_context_selection_keeps_complete_structured_tool_turn(self) -> None:
        history = SessionHistory(
            [
                Message(role="user", content="old " * 200),
                Message(role="assistant", content="old response " * 200),
                Message(role="user", content="read test.md"),
                Message(
                    role="assistant",
                    tool_calls=[
                        {
                            "call_id": "call_read",
                            "name": "read",
                            "arguments": {"path": "test.md"},
                        }
                    ],
                ),
                Message(
                    role="tool",
                    content="its a test",
                    tool_call_id="call_read",
                    name="read",
                ),
            ]
        )
        builder = ContextBuilder(max_tokens=180)

        builder.build(
            "read test.md",
            history,
            UserProfile.default_for("default"),
            [],
        )

        self.assertNotIn("old response", str(builder.last_messages))
        assistant = next(item for item in builder.last_messages if item.get("tool_calls"))
        tool = next(item for item in builder.last_messages if item.get("role") == "tool")
        self.assertEqual(assistant["tool_calls"][0]["call_id"], tool["tool_call_id"])


if __name__ == "__main__":
    unittest.main()
