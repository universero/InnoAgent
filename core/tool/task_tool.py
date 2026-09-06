"""Task tool entry point."""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, Field

from core.planning.schemas import Plan, Task, TaskStatus
from core.planning.tasks import apply_task_update, sync_steps_from_tasks
from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.decorators import tool


class TaskInput(BaseModel):
    """Arguments accepted by the task tool."""
    task_id: str | None = None
    title: str | None = None
    status: TaskStatus = "pending"
    result: str | None = None


@tool
class TaskTool(BaseTool):
    """Expose task creation and status updates as a tool."""
    name = "task"
    description = "创建任务或更新任务状态。"
    input_model = TaskInput

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

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
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
            try:
                tasks = apply_task_update(
                    tasks,
                    args["task_id"],
                    args["status"],
                    args.get("result"),
                )
                message = f"任务 {args['task_id'][:8]} 已更新为 {args['status']}"
            except KeyError as exc:
                return ToolResult(tool_name=self.name, status="error", output=str(exc))
        else:
            if not args.get("title"):
                return ToolResult(tool_name=self.name, status="error", output="新建任务需要提供 title")
            task = Task(
                task_id=uuid4().hex,
                plan_id=plan.plan_id,
                title=args["title"],
                status=args["status"],
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
