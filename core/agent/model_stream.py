"""Normalize provider events into one model response batch."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from core.event.events import AgentEvent
from core.llm import BaseModelClient


@dataclass
class ModelBatch:
    text: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class ModelStreamConsumer:
    """Convert model deltas and completed items into a deterministic batch."""

    def __init__(
        self,
        model: BaseModelClient,
        emit: Callable[[AgentEvent], None],
    ) -> None:
        self.model = model
        self.emit = emit

    def call(
        self,
        context: str,
        state: dict[str, Any],
        *,
        stage: str,
        tool_schemas: list[dict[str, Any]],
    ) -> ModelBatch:
        text_parts: list[str] = []
        completed_text = ""
        call_parts: dict[str, dict[str, Any]] = {}
        usage: dict[str, int] = {}
        error: str | None = None
        # Provider 可能将一个 tool call 拆成多个事件，按 call_id 聚合后再交给 Runtime。
        for event in self.model.stream_events(
            context,
            tool_schemas,
            stage=stage,
            state=state,
        ):
            self.emit(event)
            if event.type == "item.delta" and event.item_type == "message":
                text_parts.append(event.delta or event.content or "")
            elif event.type == "item.started" and event.item_type == "tool_call":
                call_id = event.call_id or str(len(call_parts))
                call_parts.setdefault(
                    call_id,
                    {"call_id": call_id, "name": event.tool_name or "", "arguments_text": ""},
                )
            elif event.type == "item.delta" and event.item_type == "tool_call":
                call_id = event.call_id or "0"
                call_parts.setdefault(
                    call_id,
                    {"call_id": call_id, "name": event.tool_name or "", "arguments_text": ""},
                )["arguments_text"] += event.delta or event.content or ""
            elif event.type == "item.completed" and event.item_type == "message":
                completed_text = str(event.payload.get("content") or event.content or "")
            elif event.type == "item.completed" and event.item_type == "tool_call":
                call_id = event.call_id or str(len(call_parts))
                part = call_parts.setdefault(
                    call_id,
                    {"call_id": call_id, "name": "", "arguments_text": ""},
                )
                part["name"] = event.tool_name or event.payload.get("name") or part["name"]
                part["arguments"] = event.arguments or event.payload.get("arguments") or {}
            elif event.type == "response.completed":
                usage = {
                    str(key): int(value)
                    for key, value in (event.usage or {}).items()
                    if isinstance(value, int)
                }
            elif event.type == "response.failed":
                error = str(event.payload.get("error") or event.finish_reason or "model failed")

        calls = [self._complete_call(call_id, part) for call_id, part in sorted(call_parts.items())]
        return ModelBatch(
            text="".join(text_parts).strip() or completed_text.strip(),
            calls=calls,
            usage=usage,
            error=error,
        )

    @staticmethod
    def _complete_call(call_id: str, part: dict[str, Any]) -> dict[str, Any]:
        arguments = part.get("arguments")
        if arguments is None:
            raw = str(part.get("arguments_text") or "")
            try:
                arguments = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                arguments = {"_raw": raw}
        return {
            "call_id": call_id,
            "name": str(part.get("name") or ""),
            "arguments": arguments,
        }
