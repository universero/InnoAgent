"""Progressive Skill loading tool."""

from __future__ import annotations

from pydantic import BaseModel

from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Load the full instructions for one available Skill by exact name. Use this only
when the Skill description matches the current task. The returned instructions become active
session context; do not load unrelated Skills speculatively."""


class SkillInput(BaseModel):
    name: str


@tool
class SkillTool(BaseTool):
    name = "skill"
    description = TOOL_PROMPT
    input_model = SkillInput
    parallel_safe = False

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        loader = context.services.get("skill_loader")
        if loader is None:
            return ToolResult(tool_name=self.name, status="error", output="skill loader unavailable")
        try:
            skill = loader.load(tool_input.name)
        except KeyError as exc:
            return ToolResult(tool_name=self.name, status="error", output=str(exc))
        return ToolResult(
            tool_name=self.name,
            status="success",
            output=f"Loaded skill {skill.name}\n\n{skill.content or ''}",
            data={"active_skill": skill.model_dump()},
            metadata={"path": skill.path},
        )
