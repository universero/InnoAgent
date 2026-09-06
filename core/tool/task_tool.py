"""Task tool entry point."""

from __future__ import annotations

from uuid import uuid4

from pydantic import Field, model_validator

from core.planning.schemas import Plan, Task, TaskStatus
from core.planning.tasks import apply_task_update, sync_steps_from_tasks
from core.tool.base import BaseTool, ToolContext, ToolInput, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Create a task under the active plan or update exactly one existing task. To update,
pass task_id and an explicit status; to create, omit task_id and pass title. New tasks default to
pending. Keep status factual: use in_progress when work starts, done only after verification, and
blocked only with a concrete unresolved reason in result. Do not use task state as a substitute for
performing or verifying the work."""


class TaskInput(ToolInput):
    """Arguments accepted by the task tool."""
    task_id: str | None = Field(default=None, description="Existing task id to update.")
    title: str | None = Field(default=None, description="Required title when creating a task.")
    status: TaskStatus | None = Field(
        default=None,
        description="Required update status; new tasks default to pending when omitted.",
    )
    result: str | None = Field(default=None, description="Verification result or concrete blocker.")

    @model_validator(mode="after")
    def validate_create_or_update(self) -> "TaskInput":
        if not self.task_id and not (self.title or "").strip():
            raise ValueError("title is required when task_id is omitted")
        if self.task_id and self.status is None:
            raise ValueError("status is required when task_id is provided")
        if self.status == "blocked" and not (self.result or "").strip():
            raise ValueError("blocked tasks require a concrete result")
        return self


@tool
class TaskTool(BaseTool):
    """Expose task creation and status updates as a tool."""
    name = "task"
    description = TOOL_PROMPT
    input_model = TaskInput
    parallel_safe = False

    def dynamic_description(self, context: ToolContext | None = None) -> str:
        """Expose current task statistics to the model."""
        state = context.state if context else {}
        tasks = state.get("tasks") or []
        counts: dict[str, int] = {}
        for task in tasks:
            status = task.get("status", "pending")
            counts[status] = counts.get(status, 0) + 1
        summary = ", ".join(f"{key}={value}" for key, value in counts.items())
        return f"{self.description} 当前任务统计: {summary or '无任务'}"

    def run(self, tool_input: TaskInput, context: ToolContext) -> ToolResult:
        """Create a task or update an existing task status."""
        args = tool_input.model_dump()
        plan_dict = context.state.get("plan")
        if not plan_dict:
            return ToolResult(
                tool_name=self.name,
                status="error",
                output="当前没有计划，请先调用 plan 工具。",
            )

        plan = Plan.model_validate(plan_dict)
        tasks = [Task.model_validate(item) for item in context.state.get("tasks", [])]
        if args.get("task_id"):
            status = args["status"]
            assert status is not None
            try:
                tasks = apply_task_update(
                    tasks,
                    args["task_id"],
                    status,
                    args.get("result"),
                )
                message = f"任务 {args['task_id'][:8]} 已更新为 {status}"
            except KeyError as exc:
                return ToolResult(tool_name=self.name, status="error", output=str(exc))
        else:
            task = Task(
                task_id=uuid4().hex,
                plan_id=plan.plan_id,
                title=args["title"],
                status=args.get("status") or "pending",
                result=args.get("result"),
            )
            tasks.append(task)
            message = f"已创建任务 {task.task_id[:8]}: {task.title}"

        plan = sync_steps_from_tasks(plan, tasks)
        return ToolResult(
            tool_name=self.name,
            status="success",
            output=message,
            data={
                "plan": plan.model_dump(),
                "tasks": [task.model_dump() for task in tasks],
            },
        )
