"""Responses API 流式客户端。

默认面向 DeepSeek Responses API，但兼容其他 OpenAI Responses API 服务。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

import httpx

from core.event.events import AgentEvent
from core.prompts import SYSTEM_PROMPT
from core.llm.base import BaseModelClient, ModelDecision, ToolCallDecision


def _token_count(value: Any) -> int:
    """将 Provider token 值安全收敛为非负整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _normalize_usage(value: Any) -> dict[str, int]:
    """把 Responses API 的嵌套 usage 转成内部扁平协议。"""
    usage = value if isinstance(value, dict) else {}
    input_tokens = _token_count(usage.get("input_tokens"))
    output_tokens = _token_count(usage.get("output_tokens"))
    total_value = usage.get("total_tokens")
    total_tokens = (
        _token_count(total_value)
        if isinstance(total_value, int) and not isinstance(total_value, bool)
        else input_tokens + output_tokens
    )

    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": _token_count(
            input_details.get("cached_tokens") if isinstance(input_details, dict) else None
        ),
        "reasoning_tokens": _token_count(
            output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
        ),
    }


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

    def list_models(self, timeout: float = 15.0) -> list[str]:
        """从 OpenAI 兼容的 models 端点读取可用模型。"""
        response = httpx.get(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        raw_models: Any
        if isinstance(payload, dict):
            if "data" in payload:
                raw_models = payload["data"]
            elif "models" in payload:
                raw_models = payload["models"]
            else:
                raise ValueError("模型服务返回了无法识别的模型列表")
        else:
            raw_models = payload
        if not isinstance(raw_models, list):
            raise ValueError("模型服务返回了无法识别的模型列表")

        names: set[str] = set()
        for item in raw_models:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(
                    item.get("id") or item.get("model") or item.get("name") or ""
                ).strip()
            else:
                name = ""
            if name:
                names.add(name)
        if self.model:
            names.add(self.model)
        if not names:
            raise ValueError("模型服务没有返回可用模型")
        return sorted(names, key=str.casefold)

    def respond(
        self,
        context: str,
        tool_schemas: list[dict[str, Any]],
        state: dict[str, Any] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
    ) -> ModelDecision:
        """调用 Responses API 并解析 SSE 事件流。"""
        payload = self._build_payload(context, tool_schemas, state=state)
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
        state: dict[str, Any] | None = None,
    ):
        """调用 Responses API，并将响应事件转换为内部事件流。"""
        payload = self._build_payload(context, tool_schemas, state=state)
        tool_names: dict[int, str] = {}
        tool_call_ids: dict[int, str] = {}
        completed_calls: set[int] = set()
        text_delta_seen = False
        reasoning_delta_seen = False
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
                        type="response.failed",
                        stage=stage,  # type: ignore[arg-type]
                        finish_reason=f"failed: {error}",
                        payload={"error": error},
                    )
                    return
                if event_type == "response.incomplete":
                    yield AgentEvent(
                        type="response.failed",
                        stage=stage,  # type: ignore[arg-type]
                        finish_reason="length",
                        payload={"error": "response incomplete"},
                    )
                    return
                if event_type == "response.reasoning_text.delta":
                    reasoning_delta_seen = True
                    yield AgentEvent(
                        type="item.delta",
                        is_delta=True,
                        stage=stage,  # type: ignore[arg-type]
                        item_type="reasoning",
                        delta=str(data.get("delta") or ""),
                        content=str(data.get("delta") or ""),
                    )
                elif event_type == "response.reasoning_text.done":
                    yield AgentEvent(
                        type="item.completed",
                        stage=stage,  # type: ignore[arg-type]
                        item_type="reasoning",
                        payload={
                            "content": str(data.get("text") or data.get("delta") or ""),
                            "streamed": reasoning_delta_seen,
                        },
                        content=str(data.get("text") or data.get("delta") or ""),
                    )
                elif event_type == "response.output_text.delta":
                    text_delta_seen = True
                    yield AgentEvent(
                        type="item.delta",
                        is_delta=True,
                        stage=stage,  # type: ignore[arg-type]
                        item_type="message",
                        delta=str(data.get("delta") or ""),
                        content=str(data.get("delta") or ""),
                    )
                elif event_type == "response.output_text.done":
                    yield AgentEvent(
                        type="item.completed",
                        stage=stage,  # type: ignore[arg-type]
                        item_type="message",
                        payload={
                            "content": str(data.get("text") or ""),
                            "streamed": text_delta_seen,
                        },
                        content=str(data.get("text") or ""),
                    )
                elif event_type == "response.output_item.added":
                    item = data.get("item") or {}
                    if item.get("type") == "function_call":
                        index = int(data.get("output_index", 0))
                        tool_names[index] = str(item.get("name") or "")
                        call_id = str(item.get("call_id") or item.get("id") or index)
                        tool_call_ids[index] = call_id
                        yield AgentEvent(
                            type="item.started",
                            stage=stage,  # type: ignore[arg-type]
                            item_type="tool_call",
                            call_id=call_id,
                            tool_name=str(item.get("name") or ""),
                            arguments={},
                            payload={"name": str(item.get("name") or "")},
                        )
                elif event_type == "response.function_call_arguments.delta":
                    index = int(data.get("output_index", 0))
                    yield AgentEvent(
                        type="item.delta",
                        is_delta=True,
                        stage=stage,  # type: ignore[arg-type]
                        item_type="tool_call",
                        call_id=tool_call_ids.get(index, str(index)),
                        delta=str(data.get("delta") or ""),
                        content=str(data.get("delta") or ""),
                    )
                elif event_type == "response.function_call_arguments.done":
                    index = int(data.get("output_index", 0))
                    arguments_text = str(data.get("arguments") or "")
                    try:
                        arguments = json.loads(arguments_text) if arguments_text else {}
                    except json.JSONDecodeError:
                        arguments = {"_raw": arguments_text}
                    completed_calls.add(index)
                    yield AgentEvent(
                        type="item.completed",
                        stage=stage,  # type: ignore[arg-type]
                        item_type="tool_call",
                        call_id=tool_call_ids.get(index, str(index)),
                        tool_name=tool_names.get(index, ""),
                        arguments=arguments,
                        payload={"name": tool_names.get(index, ""), "arguments": arguments},
                    )
                elif event_type == "response.output_item.done":
                    item = data.get("item") or {}
                    index = int(data.get("output_index", 0))
                    if item.get("type") == "function_call" and index not in completed_calls:
                        call_id = str(
                            item.get("call_id")
                            or item.get("id")
                            or tool_call_ids.get(index)
                            or index
                        )
                        tool_call_ids[index] = call_id
                        arguments_text = str(item.get("arguments") or "")
                        try:
                            arguments = json.loads(arguments_text) if arguments_text else {}
                        except json.JSONDecodeError:
                            arguments = {"_raw": arguments_text}
                        yield AgentEvent(
                            type="item.completed",
                            stage=stage,  # type: ignore[arg-type]
                            item_type="tool_call",
                            call_id=call_id,
                            tool_name=str(item.get("name") or tool_names.get(index, "")),
                            arguments=arguments,
                            payload={
                                "name": str(item.get("name") or tool_names.get(index, "")),
                                "arguments": arguments,
                            },
                        )
                elif event_type == "response.completed":
                    # Provider 可返回嵌套 details，不能直接穿透到严格事件模型。
                    usage = _normalize_usage((data.get("response") or {}).get("usage"))
                    yield AgentEvent(
                        type="response.completed",
                        stage=stage,  # type: ignore[arg-type]
                        finish_reason="stop",
                        usage=usage,
                    )

    def _build_payload(
        self,
        context: str,
        tool_schemas: list[dict[str, Any]],
        *,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """组装 Responses API 请求体。"""
        return {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": self._build_input(context, state),
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

    @staticmethod
    def _build_input(
        context: str,
        state: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Build Responses input while preserving function-call/output relationships."""
        messages = list((state or {}).get("messages") or [])
        if not messages:
            return [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": context}],
                }
            ]

        items: list[dict[str, Any]] = []
        runtime_context = str((state or {}).get("_runtime_context") or "").strip()
        if runtime_context:
            items.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Runtime context (not a user request):\n" + runtime_context,
                        }
                    ],
                }
            )

        for message in messages:
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role in {"user", "assistant"} and content:
                items.append(
                    {
                        "role": role,
                        "content": [
                            {
                                "type": "input_text" if role == "user" else "output_text",
                                "text": content,
                            }
                        ],
                    }
                )

            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = str(call.get("call_id") or "")
                    name = str(call.get("name") or "")
                    if not call_id or not name:
                        continue
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": json.dumps(
                                call.get("arguments") or {},
                                ensure_ascii=False,
                            ),
                        }
                    )
            elif role == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if call_id:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": content,
                        }
                    )
                elif content:
                    # 旧 session 没有 call_id，只能作为低权限上下文兼容恢复。
                    items.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Previous tool result:\n" + content,
                                }
                            ],
                        }
                    )
        return items or [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": context}],
            }
        ]

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
