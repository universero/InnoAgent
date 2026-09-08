"""Structured model-to-user question tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.llm import BaseModelClient, ModelDecision, ToolCallDecision
from core.agent.react import InnoAgent
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


class _QuestionAndApprovalModel(_QuestionModel):
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
                    ),
                    ToolCallDecision(
                        name="write",
                        arguments={"path": "choice.txt", "content": "pending"},
                    ),
                ],
            )
        return super().respond(context, tool_schemas, state, on_token, on_thinking)


class UserQuestionTest(unittest.TestCase):
    def test_ask_user_pauses_and_answer_resumes_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
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

    def test_approval_resume_preserves_a_question_from_the_same_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                RuntimeConfig(
                    workspace_root=tmp,
                    profile_root=f"{tmp}/profiles",
                    session_root=f"{tmp}/sessions",
                    mode="ask",
                    memory_enabled=False,
                ),
                model=_QuestionAndApprovalModel(),
            )

            pending = runtime.invoke("start")
            self.assertTrue(pending["pending_confirmation"])
            self.assertTrue(pending["pending_user_question"])

            approved = runtime.resolve_approval(pending["session_id"], "allow_once")

            self.assertEqual(Path(tmp, "choice.txt").read_text(), "pending")
            self.assertEqual(approved["finish_reason"], "user_input_required")
            self.assertEqual(
                approved["pending_user_question"]["question"],
                "Choose a target",
            )

            result = runtime.invoke("cli", session_id=pending["session_id"])
            self.assertEqual(result["response"], "selected: cli")


if __name__ == "__main__":
    unittest.main()
