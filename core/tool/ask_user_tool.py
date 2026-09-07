"""Structured user-question tool."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from core.tool.base import BaseTool, ToolContext, ToolInput, ToolResult
from core.tool.decorators import tool


TOOL_PROMPT = """Pause execution and ask the user one necessary question. Supply 2-5 concise,
mutually exclusive options whenever the answer can be constrained. Use allow_custom=true unless an
answer outside those options would be invalid. Do not use this tool for rhetorical questions,
status updates, confirmations already handled by permissions, or facts available through tools."""


class AskUserInput(ToolInput):
    """Arguments accepted by the structured user-question tool."""

    question: str = Field(min_length=1, description="The concise question shown to the user.")
    options: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Optional short, mutually exclusive answer choices.",
    )
    allow_custom: bool = Field(
        default=True,
        description="Whether the user may choose a custom answer.",
    )

    @field_validator("options")
    @classmethod
    def normalize_options(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if item and item not in normalized:
                normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def validate_answer_path(self) -> "AskUserInput":
        if len(self.options) == 1:
            raise ValueError("options must contain at least two choices")
        if not self.options and not self.allow_custom:
            raise ValueError("allow_custom must be true when options are empty")
        return self


@tool
class AskUserTool(BaseTool):
    """Create a resumable user-input pause without performing side effects."""

    name = "ask_user"
    description = TOOL_PROMPT
    input_model = AskUserInput
    parallel_safe = False

    def run(self, tool_input: AskUserInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            status="success",
            output="Waiting for the user's answer.",
            data={
                "user_question": {
                    "question": tool_input.question.strip(),
                    "options": tool_input.options,
                    "allow_custom": tool_input.allow_custom,
                }
            },
        )
