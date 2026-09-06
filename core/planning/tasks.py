"""Task creation and status transitions."""

from __future__ import annotations

from core.planning.schemas import Plan, PlanStep, Task, TaskStatus


def tasks_from_plan(plan: Plan) -> list[Task]:
    """Derive one executable task per non-skipped plan step."""
    tasks: list[Task] = []
    for step in plan.steps:
        if step.status == "skipped":
            continue
        tasks.append(
            Task(
                plan_id=plan.plan_id,
                title=step.title,
                status="done" if step.status == "done" else "pending",
                result=step.result,
            )
        )
    return tasks


def apply_task_update(tasks: list[Task], task_id: str, status: TaskStatus, result: str | None = None) -> list[Task]:
    """Update a task in place and mirror completion to its associated step."""
    updated = list(tasks)
    for task in updated:
        if task.task_id == task_id:
            task.status = status
            if result is not None:
                task.result = result
            return updated
    raise KeyError(f"unknown task: {task_id}")


def sync_steps_from_tasks(plan: Plan, tasks: list[Task]) -> Plan:
    """Keep plan steps consistent with task status."""
    by_title = {task.title: task for task in tasks}
    for step in plan.steps:
        task = by_title.get(step.title)
        if task is None:
            continue
        if task.status == "done":
            step.status = "done"
        elif task.status == "in_progress":
            step.status = "in_progress"
        elif task.status == "blocked":
            step.status = "blocked"
        elif task.status == "pending" and step.status not in {"blocked", "done"}:
            step.status = "pending"
    if plan.is_complete():
        plan.status = "completed"
    return plan
