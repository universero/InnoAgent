"""High-level runtime facade used by the CLI."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from core.memory.profile import ProfileStore
from core.memory.recall import MemoryRecall
from core.memory.thresholds import MemoryThresholds
from core.memory.update import MemoryUpdater
from core.runtime.config import RuntimeConfig
from core.runtime.events import make_event
from core.runtime.graph import build_main_graph
from core.llm import BaseModelClient, OpenAICompatibleModel
from core.runtime.model_config import ModelConfig, ModelConfigLoader
from core.runtime.nodes import RuntimeDependencies
from core.runtime.state import AgentState, initial_state
from core.session.context import ContextBuilder
from core.session.store import SessionRecord, SessionStore
from core.tool.registry import ToolRegistry, tool_registry
from observe.traces import TraceStore
from observe.metrics import RunMetrics


def _register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    """Ensure built-in tool classes are registered in the target registry."""
    # Importing these modules registers their classes in the shared registry.
    from core.tool import grep_tool, ls_tool, plan_tool, read_tool, task_tool, write_tool  # noqa: F401

    # The modules above are registered into tool_registry, but callers can pass
    # a custom registry; copy the currently registered instances in that case.
    if registry is not tool_registry:
        for tool in tool_registry.list_tools():
            registry.register(tool.__class__())
        if not registry._guardrails:
            registry.set_default_guardrails()
    return registry


class InnoAgentRuntime:
    """Own the graph, tools, memory and session persistence."""

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        model: BaseModelClient | None = None,
        registry: ToolRegistry | None = None,
        stream_handler: Callable[[dict[str, Any]], None] | None = None,
        model_config: ModelConfig | None = None,
        model_config_loader: ModelConfigLoader | None = None,
    ) -> None:
        """Build the runtime graph and supporting services."""
        if model is None:
            raise ValueError("InnoAgentRuntime requires an API-backed model client")
        self.config = config or RuntimeConfig()
        self.registry = _register_builtin_tools(registry or ToolRegistry())
        self.model = model
        self.model_config = model_config
        self.model_config_loader = model_config_loader or ModelConfigLoader(
            project_root=self.config.workspace_root
        )
        self.stream_handler = stream_handler
        self._run_events: list[dict[str, Any]] = []
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
        self.tracer = TraceStore()
        self.metrics = RunMetrics()
        self._deps = RuntimeDependencies(
            config=self.config,
            model=self.model,
            registry=self.registry,
            context_builder=ContextBuilder(max_tokens=self.config.max_context_tokens),
            memory_recall=self.memory_recall,
            memory_updater=self.memory_updater,
            tracer=self.tracer,
            metrics=self.metrics,
            event_handler=self._emit_event,
        )
        self.graph = build_main_graph(self._deps)

    def _emit_event(self, event: dict[str, Any]) -> None:
        """Buffer an event and forward it to the CLI stream handler."""
        self._run_events.append(event)
        if self.stream_handler:
            self.stream_handler(event)

    def new_state(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        goal: str | None = None,
    ) -> tuple[AgentState, str]:
        """Create a fresh session state and datetime-based session id."""
        sid = session_id or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        profile = self.memory_recall.recall("default")
        state = initial_state(
            user_input=user_input,
            session_id=sid,
            user_id="default",
            goal=goal,
            mode=self.config.mode,
            max_iterations=self.config.max_iterations,
            memory_profile=profile.model_dump(),
        )
        state["messages"] = [{"role": "user", "content": user_input}]
        return state, sid

    def invoke(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        goal: str | None = None,
    ) -> AgentState:
        """Run one user turn, streaming structured events as the graph executes."""
        if session_id:
            state = self.session_store.load_state(session_id)
            state["user_input"] = user_input
            state["messages"] = list(state.get("messages", [])) + [{"role": "user", "content": user_input}]
            if goal is not None:
                state["goal"] = goal
                state["goal_complete"] = False
            state["pending_confirmation"] = None
            new_session = False
        else:
            state, session_id = self.new_state(user_input, goal=goal)
            new_session = True

        state["turn_id"] = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self._run_events = []
        if new_session:
            record = SessionRecord(
                session_id=session_id,
                user_id=str(state.get("user_id", "default")),
                goal=state.get("goal"),
                mode=str(state.get("mode", self.config.mode)),
            )
            self.session_store.create(record)

        self._emit_event(
            make_event(
                "user_input",
                session_id=session_id,
                user_input=user_input,
                goal=goal,
            )
        )
        result = state
        for result in self.graph.stream(
            state,
            config={"recursion_limit": max(50, self.config.max_iterations * 4)},
            stream_mode="values",
        ):
            pass
        result["session_id"] = session_id
        self._save_session(result)
        self.memory_updater.register_turn(user_input, result.get("response", ""))
        return result

    def resume(self, session_id: str) -> AgentState:
        """Return the latest state snapshot for a session."""
        return self.session_store.load_state(session_id)

    def continue_session(self, session_id: str, user_input: str) -> AgentState:
        """Append a new user turn to an existing session."""
        return self.invoke(user_input, session_id=session_id)

    def approve_pending(self, session_id: str) -> AgentState:
        """Approve the pending guarded tool call and continue execution."""
        state = self.session_store.load_state(session_id)
        pending = state.get("pending_confirmation") or {}
        call = {
            "name": pending.get("tool_name"),
            "arguments": (pending.get("metadata") or {}).get("arguments", {}),
        }
        state["tool_results"] = [
            item
            for item in state.get("tool_results", [])
            if not (
                item.get("status") == "needs_confirmation"
                and item.get("tool_name") == call["name"]
            )
        ]
        state["approved_tool_calls"] = [call]
        state["pending_confirmation"] = None
        state["turn_id"] = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        state["messages"] = list(state.get("messages", [])) + [
            {"role": "user", "content": "用户已确认，继续执行之前请求的操作。"}
        ]
        self._run_events = []
        self._emit_event(
            make_event(
                "user_input",
                session_id=session_id,
                user_input="用户已确认，继续执行之前请求的操作。",
            )
        )
        result = state
        for result in self.graph.stream(
            state,
            config={"recursion_limit": max(50, self.config.max_iterations * 4)},
            stream_mode="values",
        ):
            pass
        result["session_id"] = session_id
        self._save_session(result)
        return result

    def set_mode(self, mode: str) -> None:
        """Change the active file-operation mode."""
        self.config.mode = mode

    def update_model(self, model_name: str, reasoning_effort: str | None = None) -> OpenAICompatibleModel:
        """更新当前模型和思考强度，并持久化到项目配置。"""
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
        self._deps.model = new_model

        if self.model_config is not None:
            self.model_config.model = model_name
            self.model_config.reasoning_effort = effort
            self.model_config_loader.save(self.model_config)
        return new_model

    def rename_session(self, session_id: str, name: str) -> SessionRecord:
        """Set the human-readable name for a session."""
        return self.session_store.rename(session_id, name)

    def _save_session(self, state: AgentState) -> None:
        """Append the current event log and finish marker to the session file."""
        session_id = str(state["session_id"])
        self._emit_event(make_event("finish", session_id=session_id))
        self.session_store.append_events(session_id, self._run_events)

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        """List recent sessions for the resume command."""
        return self.session_store.list_sessions(limit)
