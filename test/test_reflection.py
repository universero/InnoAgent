"""Reflection evaluator tests."""

from __future__ import annotations

import unittest

from core.reflection.evaluator import evaluate_goal
from core.reflection.schemas import ReflectionInput


class ReflectionTest(unittest.TestCase):
    """Tests for goal completion evaluation."""

    def test_incomplete_when_tasks_pending(self) -> None:
        """Verify pending tasks cause incomplete reflection."""
        result = evaluate_goal(
            ReflectionInput(
                goal="build app",
                response="done",
                tasks=[{"task_id": "1", "title": "work", "status": "pending"}],
            )
        )
        self.assertFalse(result.complete)
        self.assertTrue(result.feedback)

    def test_complete_when_all_tasks_done(self) -> None:
        """Verify completed tasks cause complete reflection."""
        result = evaluate_goal(
            ReflectionInput(
                goal="build app",
                response="app.py created",
                tasks=[{"task_id": "1", "title": "work", "status": "done"}],
            )
        )
        self.assertTrue(result.complete)


if __name__ == "__main__":
    unittest.main()
