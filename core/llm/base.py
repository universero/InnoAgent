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
