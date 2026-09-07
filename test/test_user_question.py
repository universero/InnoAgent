"""Structured model-to-user question tests."""

from __future__ import annotations

import tempfile
import unittest

from core.llm import BaseModelClient, ModelDecision, ToolCallDecision
from core.runtime.agent import InnoAgentRuntime
from core.runtime.config import RuntimeConfig


class _QuestionModel(BaseModelClient):
    def respond(self, context, tool_schemas, state=None, on_token=None, on_thinking=None):
        user_input = str((state or {}).get("user_input") or "")
        if user_input == "start":
            return ModelDecision(
                action="tool_use",
                tool_calls=[
                    ToolCallDecision(
                        name="ask_user",
                        arguments={
                            "question": "Choose a target",
                            "options": ["web", "cli"],
                            "allow_custom": True,
                        },
                    )
                ],
            )
        return ModelDecision(action="finish", message=f"selected: {user_input}")


class UserQuestionTest(unittest.TestCase):
    def test_ask_user_pauses_and_answer_resumes_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgentRuntime(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=f"{tmp}/profiles",
                    session_root=f"{tmp}/sessions",
                    memory_enabled=False,
                ),
                model=_QuestionModel(),
            )
            pending = runtime.invoke("start")

            self.assertEqual(
                pending["pending_user_question"],
                {
                    "question": "Choose a target",
                    "options": ["web", "cli"],
                    "allow_custom": True,
                },
            )
            self.assertEqual(pending["finish_reason"], "user_input_required")

            result = runtime.invoke("cli", session_id=pending["session_id"])

            self.assertIsNone(result["pending_user_question"])
            self.assertEqual(result["response"], "selected: cli")


if __name__ == "__main__":
    unittest.main()
