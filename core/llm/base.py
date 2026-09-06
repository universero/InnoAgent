"""模型客户端基础类型。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallDecision(BaseModel):
    """模型返回的一个工具调用。"""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelDecision(BaseModel):
    """归一化后的模型下一步动作。"""

    action: Literal["tool_use", "planning", "finish"]
    message: str = ""
    tool_calls: list[ToolCallDecision] = Field(default_factory=list)


class BaseModelClient(ABC):
    """运行时依赖的模型客户端接口。"""

    @abstractmethod
    def respond(
        self,
        context: str,
        tool_schemas: list[dict[str, Any]],
        state: dict[str, Any] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
    ) -> ModelDecision:
        """根据上下文返回下一步动作。"""

    def stream_events(
        self,
        context: str,
        tool_schemas: list[dict[str, Any]],
        *,
        stage: str = "main",
        state: dict[str, Any] | None = None,
    ):
        """Adapt a decision-based test/client implementation to runtime events."""
        from core.event.events import AgentEvent

        text_deltas: list[str] = []
        reasoning_deltas: list[str] = []
        decision = self.respond(
            context,
            tool_schemas,
            state=state,
            on_token=text_deltas.append,
            on_thinking=reasoning_deltas.append,
        )
        for delta in reasoning_deltas:
            yield AgentEvent(
                type="item.delta",
                stage=stage,  # type: ignore[arg-type]
                item_type="reasoning",
                delta=delta,
                content=delta,
                is_delta=True,
            )
        for delta in text_deltas:
            yield AgentEvent(
                type="item.delta",
                stage=stage,  # type: ignore[arg-type]
                item_type="message",
                delta=delta,
                content=delta,
                is_delta=True,
            )
        if decision.message and not text_deltas:
            yield AgentEvent(
                type="item.completed",
                stage=stage,  # type: ignore[arg-type]
                item_type="message",
                payload={"content": decision.message},
                content=decision.message,
            )
        calls = list(decision.tool_calls)
        if decision.action == "planning" and not calls:
            calls = [
                ToolCallDecision(
                    name="plan",
                    arguments={"goal": str((state or {}).get("goal") or context)},
                )
            ]
        for index, call in enumerate(calls):
            call_id = str(index)
            yield AgentEvent(
                type="item.completed",
                stage=stage,  # type: ignore[arg-type]
                item_type="tool_call",
                call_id=call_id,
                tool_name=call.name,
                arguments=call.arguments,
                payload={"name": call.name, "arguments": call.arguments},
            )
        yield AgentEvent(
            type="response.completed",
            stage=stage,  # type: ignore[arg-type]
            finish_reason="stop",
        )
