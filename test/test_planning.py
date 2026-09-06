"""Planning service tests."""

from __future__ import annotations

import unittest

from core.planning.planner import PlanningService
from core.planning.schemas import PlanningRequest, PlanStep
from core.tool.base import ToolContext
from core.tool.plan_tool import PlanInput, PlanTool


class PlanningTest(unittest.TestCase):
    """Tests for plan creation and validation."""

    def test_create_plan(self) -> None:
        """Verify a new plan contains derived tasks."""
        output = PlanningService().create_plan(PlanningRequest(goal="create a file"))
        self.assertEqual(output.plan.revision, 1)
        self.assertGreaterEqual(len(output.tasks), 1)

    def test_update_plan_preserves_done_steps(self) -> None:
        """Verify completed steps survive plan updates."""
        service = PlanningService()
        output = service.create_plan(PlanningRequest(goal="create a file"))
        output.plan.steps[0].status = "done"
        updated = service.update_plan(PlanningRequest(goal="create and verify", existing_plan=output.plan))
        self.assertEqual(updated.plan.revision, 2)
        self.assertIn("done", [step.status for step in updated.plan.steps])

    def test_dependency_cycle_is_rejected(self) -> None:
        """Verify circular dependencies are rejected."""
        service = PlanningService()
        plan = service.create_plan(PlanningRequest(goal="x")).plan
        plan.steps = [
            PlanStep(step_id="a", title="a", depends_on=["b"]),
            PlanStep(step_id="b", title="b", depends_on=["a"]),
        ]
        with self.assertRaises(ValueError):
            service._validate(plan)

    def test_plan_tool_falls_back_to_planning_service(self) -> None:
        """Verify the tool remains usable without a model-backed plan runner."""
        result = PlanTool().run(
            PlanInput(goal="create a file"),
            ToolContext(state={}),
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["plan"]["goal"], "create a file")
        self.assertGreaterEqual(len(result.data["tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
