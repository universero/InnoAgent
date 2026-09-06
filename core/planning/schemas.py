"""Plan and task domain models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


PlanStatus = Literal["draft", "active", "revised", "completed", "abandoned"]
StepStatus = Literal["pending", "in_progress", "done", "blocked", "skipped"]
TaskStatus = Literal["pending", "in_progress", "done", "blocked"]


def _now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


class PlanStep(BaseModel):
    """One step inside a plan."""
    step_id: str = Field(default_factory=lambda: uuid4().hex)
    title: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    status: StepStatus = "pending"
    result: str | None = None


class Task(BaseModel):
    """An executable task derived from plan steps."""
    task_id: str = Field(default_factory=lambda: uuid4().hex)
    plan_id: str
    title: str
    status: TaskStatus = "pending"
    assignee: str = "agent"
    result: str | None = None


class Plan(BaseModel):
    """A goal-oriented plan with ordered steps."""
    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    goal: str
    status: PlanStatus = "active"
    steps: list[PlanStep] = Field(default_factory=list)
    rationale: str = ""
    revision: int = 1
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def progress(self) -> dict[str, int]:
        """Return status counts for display and summaries."""
        counts = {status: 0 for status in StepStatus.__args__}
        for step in self.steps:
            counts[step.status] = counts.get(step.status, 0) + 1
        return counts

    def is_complete(self) -> bool:
        """Return whether every step is done."""
        return bool(self.steps) and all(step.status == "done" for step in self.steps)


class PlanningRequest(BaseModel):
    """Input accepted by the planning service."""
    goal: str
    feedback: str | None = None
    existing_plan: Plan | None = None


class PlanningOutput(BaseModel):
    """Plan, tasks and summary returned by the planning service."""
    plan: Plan
    tasks: list[Task] = Field(default_factory=list)
    message: str = ""
