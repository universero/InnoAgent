"""Planning service used by the planning subgraph and Plan tool."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from core.planning.schemas import (
    Plan,
    PlanningOutput,
    PlanningRequest,
    PlanStep,
    StepStatus,
)
from core.planning.tasks import tasks_from_plan


class PlanValidationError(ValueError):
    """Raised when a plan cannot be safely published."""


class PlanningService:
    """Create and revise plans from a goal and optional feedback.

    This deterministic planner is intentionally small.  The main Agent is the
    source of task understanding; Planning only normalises that understanding
    into a stable, inspectable structure.  A model-backed planner can replace
    this class without changing the subgraph.
    """

    def create_plan(self, request: PlanningRequest) -> PlanningOutput:
        """Create a new plan for a goal."""
        plan = Plan(goal=request.goal, status="active", revision=1)
        plan.steps = self._draft_steps(request.goal)
        self._validate(plan)
        return PlanningOutput(
            plan=plan,
            tasks=tasks_from_plan(plan),
            message=self._summarize(plan),
        )

    def update_plan(self, request: PlanningRequest) -> PlanningOutput:
        """Revise an existing plan while preserving completed steps."""
        if request.existing_plan is None:
            return self.create_plan(request)

        plan = request.existing_plan.model_copy(deep=True)
        plan.revision += 1
        plan.status = "revised"
        plan.updated_at = datetime.now(timezone.utc)
        if request.feedback:
            plan.rationale = (plan.rationale + "\n" + request.feedback).strip()

        # If the plan is already complete and no new feedback, return as is.
        if plan.is_complete() and not request.feedback:
            plan.status = "completed"
            return PlanningOutput(plan=plan, tasks=tasks_from_plan(plan), message=self._summarize(plan))

        new_steps = self._draft_steps(request.goal)
        # Preserve completed steps from the existing plan.
        merged: list[PlanStep] = []
        for existing in plan.steps:
            if existing.status == "done":
                merged.append(existing)
        for new_step in new_steps:
            if not any(step.title == new_step.title for step in merged):
                merged.append(new_step)
        plan.steps = merged or new_steps
        self._validate(plan)
        return PlanningOutput(
            plan=plan,
            tasks=tasks_from_plan(plan),
            message=self._summarize(plan),
        )

    def _draft_steps(self, goal: str) -> list[PlanStep]:
        """Produce simple, generic steps for the current planner.

        The rule-based planner avoids pretending to deeply understand arbitrary
        goals.  It creates one step for the requested work and one for
        verification; the model can refine these steps by calling Plan again.
        """
        return [
            PlanStep(
                title="完成目标",
                description=goal,
                status="pending",
            ),
            PlanStep(
                title="验证结果",
                description="检查目标产物是否符合要求",
                depends_on=[],
                status="pending",
            ),
        ]

    def _validate(self, plan: Plan) -> None:
        """Validate plan completeness, references and dependency cycles."""
        if not plan.goal.strip():
            raise PlanValidationError("plan goal must not be empty")
        if not plan.steps:
            raise PlanValidationError("plan must contain at least one step")

        step_ids = {step.step_id for step in plan.steps}
        for step in plan.steps:
            missing = [dep for dep in step.depends_on if dep not in step_ids]
            if missing:
                raise PlanValidationError(
                    f"step {step.title!r} depends on unknown steps: {missing}"
                )

        # Detect dependency cycles with a simple DFS.
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str, path: list[str]) -> None:
            """Walk dependency links and reject cycles."""
            if step_id in visiting:
                cycle = " -> ".join(path + [step_id])
                raise PlanValidationError(f"plan contains a dependency cycle: {cycle}")
            if step_id in visited:
                return
            visiting.add(step_id)
            step = next(s for s in plan.steps if s.step_id == step_id)
            for dep in step.depends_on:
                visit(dep, path + [step_id])
            visiting.remove(step_id)
            visited.add(step_id)

        for step in plan.steps:
            visit(step.step_id, [])

    def _summarize(self, plan: Plan) -> str:
        """Return a compact plan progress summary."""
        progress = plan.progress
        return (
            f"plan {plan.plan_id[:8]} revision {plan.revision}: "
            f"{progress.get('done', 0)} done, "
            f"{progress.get('in_progress', 0)} in_progress, "
            f"{progress.get('pending', 0)} pending"
        )
