"""事件驱动的简单 ReAct Agent。"""

from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable
from datetime import datetime
from typing import Any

from core.event.events import AgentEvent
from core.llm import BaseModelClient, OpenAICompatibleModel
from core.memory.profile import ProfileStore
from core.memory.recall import MemoryRecall
from core.memory.thresholds import MemoryThresholds
from core.memory.update import MemoryUpdater
from core.runtime.config import RuntimeConfig
from core.runtime.events import make_event
from core.runtime.model_config import ModelConfig, ModelConfigLoader
from core.runtime.state import AgentState, initial_state
from core.session.context import ContextBuilder
from core.session.history import SessionHistory
from core.session.store import SessionRecord, SessionStore
from core.tool.base import ToolContext
from core.tool.registry import ToolRegistry, tool_registry


def _register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    """注册内置工具。"""
    from core.tool import grep_tool, ls_tool, plan_tool, read_tool, task_tool, write_tool  # noqa: F401

    if registry is not tool_registry:
        for item in tool_registry.list_tools():
            registry.register(item.__class__())
        if not registry._guardrails:
            registry.set_default_guardrails()
    return registry


class EventDrivenAgent:
    """维护简单 ReAct 循环并驱动事件管线。"""

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        model: BaseModelClient | None = None,
        registry: ToolRegistry | None = None,
        stream_handler: Callable[[dict[str, Any]], None] | None = None,
        model_config: ModelConfig | None = None,
        model_config_loader: ModelConfigLoader | None = None,
    ) -> None:
        if model is None:
            raise ValueError("EventDrivenAgent requires a model client")
        self.config = config or RuntimeConfig()
        self.registry = _register_builtin_tools(registry or ToolRegistry())
        self.model = model
        self.stream_handler = stream_handler
        self.model_config = model_config
        self.model_config_loader = model_config_loader or ModelConfigLoader(
            project_root=self.config.workspace_root
        )
        self.profile_store = ProfileStore(self.config.profile_root)
        self.session_store = SessionStore(self.config.session_root)
        self.memory_recall = MemoryRecall(self.profile_store)
        self.memory_updater = MemoryUpdater(
            self.profile_store,
            MemoryThresholds(
                turn_threshold=self.config.turn_threshold,
                input_token_threshold=self.config.input_token_threshold,
            ),
            enabled=self.config.memory_enabled,
        )
        self.context_builder = ContextBuilder(max_tokens=self.config.max_context_tokens)
        self._run_events: list[Any] = []

    def _emit(self, event: AgentEvent) -> None:
        """把 AgentEvent 写入当前事件缓冲并转发渲染层。"""
        self._run_events.append(event)
        if self.stream_handler:
            self.stream_handler(event.as_event_dict())

    def _emit_dict(self, event: dict[str, Any]) -> None:
        """转发 session 级别的非模型事件。"""
        self._run_events.append(event)
        if self.stream_handler:
            self.stream_handler(event)

    def invoke(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        goal: str | None = None,
    ) -> AgentState:
        """执行一个用户回合。"""
        if session_id:
            state = self.session_store.load_state(session_id)
            state["user_input"] = user_input
            state["messages"] = list(state.get("messages", [])) + [
                {"role": "user", "content": user_input}
            ]
        else:
            state, session_id = self._new_state(user_input, goal=goal)

        state["turn_id"] = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self._run_events = []
        self._emit_dict(
            make_event("user_input", session_id=session_id, user_input=user_input)
        )
        result = self._run_react(state)
        result["session_id"] = session_id
        self._save_session(result)
        return result

    def _new_state(self, user_input: str, goal: str | None) -> tuple[AgentState, str]:
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        profile = self.memory_recall.recall("default")
        state = initial_state(
            user_input=user_input,
            session_id=session_id,
            goal=goal,
            mode=self.config.mode,
            max_iterations=self.config.max_iterations,
            memory_profile=profile.model_dump(),
        )
        state["messages"] = [{"role": "user", "content": user_input}]
        self.session_store.create(
            SessionRecord(
                session_id=session_id,
                user_id="default",
                goal=goal,
                mode=self.config.mode,
            )
        )
        return state, session_id

    def _run_react(self, state: AgentState) -> AgentState:
        """简单 ReAct 循环：模型事件 -> 工具执行 -> 再次模型调用。"""
        for _ in range(int(state.get("max_iterations", 20))):
            context = self.context_builder.build(
                user_input=str(state.get("user_input", "")),
                history=SessionHistory.from_dicts(state.get("messages", [])),
                profile=self.memory_recall.recall(str(state.get("user_id", "default"))),
                tool_schemas=self.registry.tool_schemas(
                    ToolContext(
                        mode=self.config.normalized_mode,
                        allowed_roots=self.config.allowed_roots,
                        state=dict(state),
                        approved_tool_calls=state.get("approved_tool_calls", []),
                    )
                ),
                plan=state.get("plan"),
                tasks=state.get("tasks", []),
            )
            tool_call = self._consume_model_events(context, state)
            if tool_call is None:
                break
            if self._execute_tool(state, tool_call):
                return state
        return state

    def _consume_model_events(self, context: str, state: AgentState) -> dict[str, Any] | None:
        """消费模型事件流，组装一个完整工具调用。"""
        tool_name = ""
        arguments_text = ""
        final_text = ""
        for event in self.model.stream_events(
            context,
            self.registry.tool_schemas(
                ToolContext(
                    mode=self.config.normalized_mode,
                    allowed_roots=self.config.allowed_roots,
                    state=dict(state),
                    approved_tool_calls=state.get("approved_tool_calls", []),
                )
            ),
            stage="main",
        ):
            self._emit(event)
            if event.type == "reasoning":
                continue
            if event.type == "text":
                final_text += event.content or ""
            elif event.type == "tool_call":
                tool_name = event.tool_name or ""
            elif event.type == "tool_call_argument":
                arguments_text += event.content or ""
            elif event.type == "finish":
                break

        if tool_name:
            try:
                arguments = json.loads(arguments_text) if arguments_text.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
            return {"name": tool_name, "arguments": arguments}
        state["response"] = final_text
        state["streamed_response"] = final_text
        state["finished"] = True
        return None

    def _execute_tool(self, state: AgentState, tool_call: dict[str, Any]) -> bool:
        """执行工具，返回是否因为需要确认而中断。"""
        context = ToolContext(
            mode=self.config.normalized_mode,
            allowed_roots=self.config.allowed_roots,
            state=dict(state),
            approved_tool_calls=state.get("approved_tool_calls", []),
        )
        result = self.registry.execute_tool(
            tool_call["name"],
            tool_call["arguments"],
            context,
        )
        event = AgentEvent(
            type="tool_result",
            is_delta=False,
            stage="main",
            tool_name=tool_call["name"],
            arguments=tool_call["arguments"],
            result=result.model_dump(),
        )
        self._emit(event)
        state["tool_results"] = list(state.get("tool_results", [])) + [result.model_dump()]
        state["messages"] = list(state.get("messages", [])) + [
            {"role": "tool", "content": result.output or result.status}
        ]
        if result.status == "needs_confirmation":
            state["pending_confirmation"] = result.model_dump()
            permission_event = AgentEvent(
                type="permission_confirm",
                is_delta=False,
                stage="main",
                tool_name=tool_call["name"],
                arguments=tool_call["arguments"],
                content=result.output,
                result=result.model_dump(),
            )
            self._emit(permission_event)
            return True
        return False

    def _save_session(self, state: AgentState) -> None:
        """只落盘完整事件，不落盘 delta。"""
        session_id = str(state["session_id"])
        events: list[dict[str, Any]] = []
        for item in self._run_events:
            if isinstance(item, AgentEvent):
                if item.is_delta:
                    continue
                events.append(item.as_event_dict())
            else:
                events.append(item)
        self._emit_dict(make_event("finish", session_id=session_id))
        events.append({"type": "finish", "session_id": session_id})
        self.session_store.append_events(session_id, events)

    def set_mode(self, mode: str) -> None:
        self.config.mode = mode

    def update_model(self, model_name: str, reasoning_effort: str | None = None) -> OpenAICompatibleModel:
        if not isinstance(self.model, OpenAICompatibleModel):
            raise TypeError("当前模型客户端不支持动态更新")
        effort = reasoning_effort or self.model.reasoning_effort or "none"
        if effort not in {"none", "low", "high", "max"}:
            raise ValueError("reasoning_effort 必须是 none、low、high 或 max")
        new_model = OpenAICompatibleModel(
            api_key=self.model.api_key,
            base_url=self.model.base_url,
            model=model_name,
            reasoning_effort=effort,
        )
        self.model = new_model
        if self.model_config:
            self.model_config.model = model_name
            self.model_config.reasoning_effort = effort
            self.model_config_loader.save(self.model_config)
        return new_model

    def resume(self, session_id: str) -> AgentState:
        return self.session_store.load_state(session_id)

    def approve_pending(self, session_id: str) -> AgentState:
        state = self.session_store.load_state(session_id)
        pending = state.get("pending_confirmation") or {}
        call = {
            "name": pending.get("tool_name"),
            "arguments": (pending.get("metadata") or {}).get("arguments", {}),
        }
        self._record_permission(call)
        state["approved_tool_calls"] = [call]
        state["pending_confirmation"] = None
        state["messages"] = list(state.get("messages", [])) + [
            {"role": "user", "content": "用户已确认，继续执行之前请求的操作。"}
        ]
        return self._run_react(state)

    def _record_permission(self, call: dict[str, Any]) -> None:
        """把已批准的权限确认写入当前项目配置。"""
        permission_path = Path(self.config.workspace_root) / ".innoagent" / "permissions.json"
        permission_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if permission_path.exists():
            try:
                data = json.loads(permission_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        approved = data.setdefault("approved", [])
        if call not in approved:
            approved.append(call)
        permission_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def rename_session(self, session_id: str, name: str) -> SessionRecord:
        return self.session_store.rename(session_id, name)

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        return self.session_store.list_sessions(limit)
