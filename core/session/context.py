"""Assemble the final context sent to the model."""

from __future__ import annotations

from typing import Any

from core.memory.profile import UserProfile
from core.session.compression import estimate_tokens, trim_messages
from core.session.history import Message, SessionHistory


class ContextBuilder:
    """Build a single context string from all agent state."""

    def __init__(self, max_tokens: int = 4000) -> None:
        """Store the context token budget."""
        self.max_tokens = max_tokens

    def build(
        self,
        user_input: str,
        history: SessionHistory,
        profile: UserProfile,
        tool_schemas: list[dict[str, Any]],
        plan: dict | None = None,
        tasks: list[dict] | None = None,
        reflection_feedback: str | None = None,
    ) -> str:
        """Assemble the model context and trim history when necessary."""
        memory_text = profile.recall_text()
        sections = [
            f"用户输入：{user_input}",
        ]
        if memory_text:
            sections.append("用户记忆：\n" + memory_text)

        recent_messages = trim_messages(history.tail(40), self.max_tokens // 3)
        if recent_messages:
            history_block = "\n".join(
                f"{message.role}: {message.content}" for message in recent_messages[-8:]
            )
            sections.append("最近对话：\n" + history_block)

        if plan:
            sections.append(self._plan_block(plan, tasks or []))
        if reflection_feedback:
            sections.append("反思反馈：\n" + reflection_feedback)

        tool_block = "\n".join(
            f"- {schema['name']}: {schema['description']}" for schema in tool_schemas
        )
        sections.append("可用工具：\n" + tool_block)
        context = "\n\n".join(sections)
        if estimate_tokens(context) > self.max_tokens:
            context = context[: self.max_tokens * 4]
        return context

    def _plan_block(self, plan: dict, tasks: list[dict]) -> str:
        """Render plan and task state for the model context."""
        status = plan.get("status", "active")
        steps = plan.get("steps", [])
        lines = [f"当前计划：revision={plan.get('revision', 1)} status={status}"]
        for step in steps:
            lines.append(f"- [{step.get('status', 'pending')}] {step.get('title')}")
        if tasks:
            lines.append("任务：")
            for task in tasks:
                lines.append(
                    f"- [{task.get('status', 'pending')}] {task.get('task_id', '')[:8]} {task.get('title')}"
                )
        return "\n".join(lines)
