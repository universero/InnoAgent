"""InnoAgent 内部事件。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


EventType = Literal[
    "reasoning",
    "text",
    "tool_call",
    "tool_call_argument",
    "tool_result",
    "compact_start",
    "compacting",
    "compact_end",
    "finish",
]

Stage = Literal["main", "plan", "reflect"]


class AgentEvent(BaseModel):
    """事件管线中的统一事件对象。"""

    type: EventType
    is_delta: bool = False
    usage: dict[str, int] | None = None
    stage: Stage = "main"
    finish_reason: str | None = None
    content: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    progress: float | None = None
    options: list[str] | None = None

    def as_event_dict(self) -> dict[str, Any]:
        """转为可序列化的事件字典。"""
        return self.model_dump(exclude_none=True)
