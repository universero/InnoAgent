"""Plan tool entry point."""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.planning.graph import planning_graph
from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.decorators import tool


class PlanInput(BaseModel):
    """Arguments accepted by the plan tool."""
    goal: str
    feedback: str | None = None


@tool
class PlanTool(BaseTool):
    """Expose plan creation and revision as a tool."""
    name = "plan"
    description = "创建或修改计划。调用后会更新当前计划与任务进度。"
    input_model = PlanInput

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

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        """Create or update the active plan through the planning subgraph."""
        args = tool_input.model_dump()
        payload = {
            "goal": args["goal"],
            "feedback": args.get("feedback"),
            "existing_plan": context.state.get("plan"),
        }
        output = planning_graph.invoke(payload)
        return ToolResult(
            tool_name=self.name,
            status="success",
            output=output.get("message", "计划已更新"),
            data={
                "plan": output.get("plan"),
                "tasks": output.get("tasks", []),
            },
        )
