"""Plan tool entry point."""

from __future__ import annotations

from pydantic import Field

from core.planning.planner import PlanningService
from core.planning.schemas import PlanningRequest
from core.tool.base import BaseTool, ToolContext, ToolInput, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Create or revise the active execution plan through the dedicated Planning graph.
Use this for multi-step work when sequencing, dependencies, verification, or visible progress adds
value; do not use it for one obvious action. Pass the complete goal and include concrete feedback
when revising. This tool plans only: it does not execute commands or modify files."""


class PlanInput(ToolInput):
    """Arguments accepted by the plan tool."""
    goal: str = Field(min_length=1, description="Complete goal the plan must satisfy.")
    feedback: str | None = Field(
        default=None,
        description="Specific reason the existing plan must be revised.",
    )


@tool
class PlanTool(BaseTool):
    """Expose plan creation and revision as a tool."""
    name = "plan"
    description = TOOL_PROMPT
    input_model = PlanInput
    parallel_safe = False

    def dynamic_description(self, context: ToolContext | None = None) -> str:
        """Include current plan state in the model-facing description."""
        state = context.state if context else {}
        plan = state.get("plan")
        if not plan:
            return self.description
        steps = plan.get("steps", [])
        return (
            f"{self.description} 当前计划: {plan.get('status', 'active')} "
            f"rev{plan.get('revision', 1)}，共 {len(steps)} 个步骤。"
        )

    def run(self, tool_input: PlanInput, context: ToolContext) -> ToolResult:
        """Create or update the active plan through the configured planner."""
        args = tool_input.model_dump()
        runner = context.services.get("plan_runner")
        if runner is not None:
            output = runner(
                args["goal"],
                args.get("feedback"),
                context.state.get("plan"),
            )
            return ToolResult(
                tool_name=self.name,
                status="success",
                output=output.get("message", "计划已更新"),
                data={
                    "plan": output.get("plan"),
                    "tasks": output.get("tasks", []),
                    "usage": output.get("usage", {}),
                },
                warnings=output.get("warnings", []),
            )
        request = PlanningRequest(
            goal=args["goal"],
            feedback=args.get("feedback"),
            existing_plan=context.state.get("plan"),
        )
        service = PlanningService()
        output = (
            service.update_plan(request)
            if request.existing_plan is not None
            else service.create_plan(request)
        )
        return ToolResult(
            tool_name=self.name,
            status="success",
            output=output.message or "计划已更新",
            data={
                "plan": output.plan.model_dump(mode="json"),
                "tasks": [task.model_dump(mode="json") for task in output.tasks],
            },
        )
