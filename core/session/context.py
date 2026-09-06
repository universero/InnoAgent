"""Assemble the final context sent to the model."""

from __future__ import annotations

from typing import Any

from core.memory.profile import UserProfile
from core.prompts import SYSTEM_PROMPT
from core.session.compression import estimate_tokens, trim_messages
from core.session.history import Message, SessionHistory


class ContextBuilder:
    """Build a single context string from all agent state."""

    def __init__(self, max_tokens: int = 4000) -> None:
        """Store the context token budget."""
        self.max_tokens = max_tokens
        self.last_usage: dict[str, Any] = {}

    def build(
        self,
        user_input: str,
        history: SessionHistory,
        profile: UserProfile,
        tool_schemas: list[dict[str, Any]],
        plan: dict | None = None,
        tasks: list[dict] | None = None,
        reflection_feedback: str | None = None,
        context_summary: str | None = None,
        skill_index: str | None = None,
        active_skills: list[dict[str, Any]] | None = None,
    ) -> str:
        """Assemble the model context and trim history when necessary."""
        memory_text = profile.recall_text()
        sections = [
            f"用户输入：{user_input}",
        ]
        if memory_text:
            sections.append("用户记忆：\n" + memory_text)

        if context_summary:
            sections.append("压缩后的历史摘要：\n" + context_summary)

        history_messages = history.tail(40)
        if context_summary:
            history_messages = [
                message for message in history_messages if message.role != "summary"
            ]
        recent_messages = trim_messages(history_messages, self.max_tokens // 3)
        if recent_messages:
            history_block = "\n".join(
                f"{message.role}: {message.content}" for message in recent_messages[-8:]
            )
            sections.append("最近对话：\n" + history_block)

        if plan:
            sections.append(self._plan_block(plan, tasks or []))
        if reflection_feedback:
            sections.append("反思反馈：\n" + reflection_feedback)

        if skill_index:
            sections.append(skill_index)
        for skill in active_skills or []:
            content = str(skill.get("content") or "").strip()
            if content:
                sections.append(
                    f"已激活 Skill: {skill.get('name', 'unknown')}\n{content}"
                )

        tool_block = "\n".join(
            f"- {schema['name']}: {schema['description']}" for schema in tool_schemas
        )
        sections.append("可用工具：\n" + tool_block)
        context = "\n\n".join(sections)
        # 最终硬截断是预算兜底；正常情况下历史裁剪应先释放大部分空间。
        if estimate_tokens(context) > self.max_tokens:
            context = context[: self.max_tokens * 4]
        used = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(context)
        self.last_usage = {
            "used_tokens": used,
            "max_tokens": self.max_tokens,
            "remaining_tokens": max(0, self.max_tokens - used),
            "percent_used": round(min(1.0, used / max(1, self.max_tokens)) * 100, 1),
        }
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
