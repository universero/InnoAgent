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


if __name__ == "__main__":
    unittest.main()
