"""Responses API 流式客户端。

默认面向 DeepSeek Responses API，但兼容其他 OpenAI Responses API 服务。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

import httpx

from core.event.events import AgentEvent
from core.llm.base import BaseModelClient, ModelDecision, ToolCallDecision


class OpenAICompatibleModel(BaseModelClient):
    """兼容 OpenAI Responses API 的流式客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        reasoning_effort: str = "none",
    ) -> None:
        """保存 Responses API 连接参数。"""
        if not api_key.strip():
            raise ValueError("Responses API 客户端需要 API Key")
        self.api_key = api_key.strip()
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.model = model or "deepseek-v4-flash"
        self.reasoning_effort = reasoning_effort or "none"

    def respond(
        self,
        context: str,
        tool_schemas: list[dict[str, Any]],
        state: dict[str, Any] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
    ) -> ModelDecision:
        """调用 Responses API 并解析 SSE 事件流。"""
        payload = self._build_payload(context, tool_schemas)
        with httpx.stream(
            "POST",
            f"{self.base_url}/responses",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=60,
        ) as response:
            response.raise_for_status()
            parsed = self._parse_stream(
                response,
                on_token=on_token,
                on_thinking=on_thinking,
            )
        return self._build_decision(parsed["content"], parsed["tool_call_parts"])

    def stream_events(
        self,
        context: str,
        tool_schemas: list[dict[str, Any]],
        *,
        stage: str = "main",
    ):
        """调用 Responses API，并将响应事件转换为内部事件流。"""
        payload = self._build_payload(context, tool_schemas)
        with httpx.stream(
            "POST",
            f"{self.base_url}/responses",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=60,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if not data_text or data_text == "[DONE]":
                    continue
                data = json.loads(data_text)
                event_type = data.get("type") or data.get("event") or ""
                if event_type == "response.failed":
                    error = (data.get("response") or {}).get("error") or data.get("error")
                    yield AgentEvent(
                        type="finish",
                        is_delta=False,
                        stage=stage,  # type: ignore[arg-type]
                        finish_reason=f"failed: {error}",
                    )
                    return
                if event_type == "response.incomplete":
                    yield AgentEvent(
                        type="finish",
                        is_delta=False,
                        stage=stage,  # type: ignore[arg-type]
                        finish_reason="length",
                    )
                    return
                if event_type == "response.reasoning_text.delta":
                    yield AgentEvent(
                        type="reasoning",
                        is_delta=True,
                        stage=stage,  # type: ignore[arg-type]
                        content=str(data.get("delta") or ""),
                    )
                elif event_type == "response.reasoning_text.done":
                    yield AgentEvent(
                        type="reasoning",
                        is_delta=False,
                        stage=stage,  # type: ignore[arg-type]
                        content=str(data.get("text") or data.get("delta") or ""),
                    )
                elif event_type == "response.output_text.delta":
                    yield AgentEvent(
                        type="text",
                        is_delta=True,
                        stage=stage,  # type: ignore[arg-type]
                        content=str(data.get("delta") or ""),
                    )
                elif event_type == "response.output_text.done":
                    yield AgentEvent(
                        type="text",
                        is_delta=False,
                        stage=stage,  # type: ignore[arg-type]
                        content=str(data.get("text") or ""),
                    )
                elif event_type == "response.output_item.added":
                    item = data.get("item") or {}
                    if item.get("type") == "function_call":
                        yield AgentEvent(
                            type="tool_call",
                            is_delta=True,
                            stage=stage,  # type: ignore[arg-type]
                            tool_name=str(item.get("name") or ""),
                            arguments={},
                        )
                elif event_type == "response.function_call_arguments.delta":
                    yield AgentEvent(
                        type="tool_call_argument",
                        is_delta=True,
                        stage=stage,  # type: ignore[arg-type]
                        content=str(data.get("delta") or ""),
                    )
                elif event_type == "response.function_call_arguments.done":
                    yield AgentEvent(
                        type="tool_call_argument",
                        is_delta=False,
                        stage=stage,  # type: ignore[arg-type]
                        content=str(data.get("arguments") or ""),
                    )
                elif event_type == "response.completed":
                    usage = (data.get("response") or {}).get("usage")
                    yield AgentEvent(
                        type="finish",
                        is_delta=False,
                        stage=stage,  # type: ignore[arg-type]
                        finish_reason="stop",
                        usage=usage,
                    )

    def _build_payload(self, context: str, tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
        """组装 Responses API 请求体。"""
        return {
            "model": self.model,
            "instructions": (
                "你是 InnoAgent。需要执行操作时直接调用对应工具；"
            ),
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": context}],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                }
                for schema in tool_schemas
            ],
            "reasoning": {"effort": self.reasoning_effort},
            "stream": True,
        }

    def _parse_stream(
        self,
        response: Any,
        *,
        on_token: Callable[[str], None] | None,
        on_thinking: Callable[[str], None] | None,
    ) -> dict[str, Any]:
        """逐行解析 SSE，聚合文本、思维链和工具调用参数。"""
        content_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, str]] = {}
        reasoning_text_seen = False
        reasoning_summary_seen = False
        streaming_decided = False
        streaming_active = True

        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data_text = line[5:].strip()
            if not data_text or data_text == "[DONE]":
                continue
            data = json.loads(data_text)
            event_type = data.get("type") or data.get("event") or ""

            # 错误结束事件
            if event_type == "response.failed":
                error = (data.get("response") or {}).get("error") or data.get("error")
                raise RuntimeError(f"Responses API 调用失败: {error}")
            if event_type == "response.incomplete":
                raise RuntimeError("Responses API 返回 incomplete，输出被截断")

            # 思维链增量
            if event_type == "response.reasoning_text.delta":
                reasoning_text_seen = True
                chunk = str(data.get("delta") or "")
                if chunk and on_thinking:
                    on_thinking(chunk)
            elif event_type == "response.reasoning_text.done":
                if not reasoning_text_seen:
                    chunk = str(data.get("text") or data.get("delta") or "")
                    if chunk and on_thinking:
                        on_thinking(chunk)

            # 思维链摘要增量
            elif event_type == "response.reasoning_summary_text.delta":
                reasoning_summary_seen = True
                chunk = str(data.get("delta") or "")
                if chunk and on_thinking:
                    on_thinking(chunk)
            elif event_type == "response.reasoning_summary_text.done":
                if not reasoning_summary_seen:
                    chunk = str(data.get("text") or data.get("delta") or "")
                    if chunk and on_thinking:
                        on_thinking(chunk)

            # 最终输出文本增量
            elif event_type == "response.output_text.delta":
                token = str(data.get("delta") or "")
                if not token:
                    continue
                content_parts.append(token)
                if not streaming_decided:
                    probe = "".join(content_parts).lstrip()
                    if probe:
                        streaming_decided = True
                        streaming_active = not probe.startswith("{")
                if streaming_active and on_token:
                    on_token(token)

            # 工具调用 item 开始
            elif event_type == "response.output_item.added":
                item = data.get("item") or {}
                if item.get("type") != "function_call":
                    continue
                index = int(data.get("output_index", 0))
                tool_call_parts.setdefault(
                    index,
                    {"name": str(item.get("name") or ""), "arguments": ""},
                )

            # 工具调用 item 完成，可补齐名称和参数
            elif event_type == "response.output_item.done":
                item = data.get("item") or {}
                if item.get("type") != "function_call":
                    continue
                index = int(data.get("output_index", 0))
                part = tool_call_parts.setdefault(
                    index,
                    {"name": str(item.get("name") or ""), "arguments": ""},
                )
                if item.get("name"):
                    part["name"] = str(item["name"])
                if item.get("arguments"):
                    part["arguments"] = str(item["arguments"])

            # 工具参数增量
            elif event_type == "response.function_call_arguments.delta":
                index = int(data.get("output_index", 0))
                part = tool_call_parts.setdefault(index, {"name": "", "arguments": ""})
                part["arguments"] += str(data.get("delta") or "")

            # 工具参数完整值
            elif event_type == "response.function_call_arguments.done":
                index = int(data.get("output_index", 0))
                part = tool_call_parts.setdefault(index, {"name": "", "arguments": ""})
                if data.get("arguments"):
                    part["arguments"] = str(data["arguments"])

            # 输出文本完整值
            elif event_type == "response.output_text.done":
                if not content_parts and data.get("text"):
                    text = str(data["text"])
                    content_parts.append(text)
                    if on_token and not text.lstrip().startswith("{"):
                        on_token(text)

            # 正常结束事件，补充最终 output
            elif event_type == "response.completed":
                self._fill_from_completed(
                    data,
                    content_parts,
                    tool_call_parts,
                    on_token=on_token,
                )

        return {
            "content": "".join(content_parts).strip(),
            "tool_call_parts": tool_call_parts,
        }

    def _fill_from_completed(
        self,
        data: dict[str, Any],
        content_parts: list[str],
        tool_call_parts: dict[int, dict[str, str]],
        *,
        on_token: Callable[[str], None] | None,
    ) -> None:
        """用 response.completed 中的最终 output 补齐缺失数据。"""
        completed_response = data.get("response") or {}
        output_items = completed_response.get("output") or []
        for index, item in enumerate(output_items):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" and not content_parts:
                text = "".join(
                    str(block.get("text") or block.get("input_text") or "")
                    for block in (item.get("content") or [])
                    if isinstance(block, dict)
                )
                if text:
                    content_parts.append(text)
                    if on_token and not text.lstrip().startswith("{"):
                        on_token(text)
            elif item.get("type") == "function_call" and index not in tool_call_parts:
                tool_call_parts[index] = {
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or ""),
                }

    def _build_decision(
        self,
        content: str,
        tool_call_parts: dict[int, dict[str, str]],
    ) -> ModelDecision:
        """将聚合后的文本和工具调用转换为 ModelDecision。"""
        tool_calls: list[ToolCallDecision] = []
        for index in sorted(tool_call_parts):
            part = tool_call_parts[index]
            arguments_text = part["arguments"].strip()
            try:
                arguments = json.loads(arguments_text) if arguments_text else {}
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(ToolCallDecision(name=part["name"], arguments=arguments))

        parsed_action: Literal["tool_use", "planning", "finish"] | None = None
        parsed_message: str | None = None
        parsed_tool_calls: list[ToolCallDecision] | None = None
        if content.startswith("{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    parsed_message = str(parsed.get("message") or "")
                    raw_action = parsed.get("action")
                    if raw_action in {"tool_use", "planning", "finish"}:
                        parsed_action = raw_action
                    raw_tool_calls = parsed.get("tool_calls") or []
                    if isinstance(raw_tool_calls, list):
                        parsed_tool_calls = [
                            ToolCallDecision(
                                name=str(item.get("name") or ""),
                                arguments=item.get("arguments") or {},
                            )
                            for item in raw_tool_calls
                            if isinstance(item, dict)
                        ]
            except json.JSONDecodeError:
                pass

        final_tool_calls = parsed_tool_calls if parsed_tool_calls is not None else tool_calls
        if final_tool_calls:
            action = (
                "planning"
                if any(call.name == "plan" for call in final_tool_calls)
                else "tool_use"
            )
        elif parsed_action is not None:
            action = parsed_action if parsed_action != "tool_use" else "finish"
        else:
            action = "finish"
        return ModelDecision(
            action=action,
            message=parsed_message if parsed_message is not None else content,
            tool_calls=final_tool_calls,
        )
