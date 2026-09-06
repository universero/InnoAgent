"""Stable runtime events shared by the model, agent, session, and UI."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


Stage = Literal["main", "plan", "reflect", "compact", "subagent"]


class AgentEvent(BaseModel):
    """事件管线中的统一事件对象。"""

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    type: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    session_id: str | None = None
    turn_id: str | None = None
    stage: Stage = "main"
    item_id: str | None = None
    item_type: str | None = None
    call_id: str | None = None
    delta: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, int] | None = None

    # Compatibility fields used by older adapters and session logs.
    is_delta: bool = False
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

    @property
    def persistent(self) -> bool:
        """Delta events are display-only; every other event is replayable."""
        return self.type != "item.delta" and not self.is_delta
