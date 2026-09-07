"""The single event-driven ReAct runtime used by InnoAgent."""

from __future__ import annotations

import json
from datetime import datetime
from threading import Lock
from typing import Any, Callable

from core.agent.graph import MainAgentGraph
from core.agent.model_stream import ModelBatch, ModelStreamConsumer
from core.agent.stages import StageRunner
from core.agent.subagent import SubagentRunner
from core.config.permissions import PermissionStore
from core.event.events import AgentEvent
from core.llm import BaseModelClient, OpenAICompatibleModel
from core.memory.profile import ProfileStore
from core.memory.recall import MemoryRecall
from core.memory.thresholds import MemoryThresholds
from core.memory.update import MemoryUpdater
from core.runtime.config import RuntimeConfig, RuntimeConfigStore
from core.runtime.model_config import ModelConfig, ModelConfigLoader
from core.runtime.state import AgentState, initial_state, reset_goal_scope, reset_turn_scope
from core.runtime.usage import accumulate_usage, normalize_usage
from core.session.compression import ContextCompactor, estimate_message_tokens
from core.session.context import ContextBuilder
from core.session.history import SessionHistory
from core.session.store import SessionRecord, SessionStore
from core.skill.loader import SkillLoader
from core.tool.approval import (
    VALID_APPROVAL_DECISIONS,
    ApprovalDecision,
    ApprovalRequest,
)
from core.tool.base import ToolAuthorization, ToolContext, ToolResult
from core.tool.registry import ToolRegistry, tool_registry


_GOAL_UNSET = object()


def _register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    """Import and register all built-in tools exactly once."""
    from core.tool import (  # noqa: F401
        ask_user_tool,
        grep_tool,
        ls_tool,
        plan_tool,
        read_tool,
        shell_tool,
        skill_tool,
        subagent_tool,
        task_tool,
        write_tool,
    )

    if registry is not tool_registry:
        for item in tool_registry.list_tools():
            registry.register(item.__class__())
        if not registry._guardrails:
            registry.set_default_guardrails()
    return registry


class EventDrivenAgent:
    """Own the model loop, tools, permissions, context, and session events."""

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        model: BaseModelClient | None = None,
        registry: ToolRegistry | None = None,
        stream_handler: Callable[[dict[str, Any]], None] | None = None,
        model_config: ModelConfig | None = None,
        model_config_loader: ModelConfigLoader | None = None,
        summarizer: Callable[[str], str] | None = None,
    ) -> None:
        if model is None:
            raise ValueError("EventDrivenAgent requires a model client")
        self.config = config or RuntimeConfig()
        self.runtime_config_store = RuntimeConfigStore(self.config.workspace_root)
        self.registry = _register_builtin_tools(registry or ToolRegistry())
        self.model = model
        self.stream_handler = stream_handler
        self.model_config = model_config
        self.model_config_loader = model_config_loader or ModelConfigLoader(
            project_root=self.config.workspace_root
        )
        self.profile_store = ProfileStore(self.config.profile_root)
        self.session_store = SessionStore(self.config.session_root)
        self.permission_store = PermissionStore(self.config.workspace_root)
        self.skill_loader = SkillLoader(self.config.workspace_root)
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
        self.compactor = ContextCompactor(self.config.compact_keep_recent_tokens)
        self.model_stream = ModelStreamConsumer(self.model, self._emit)
        self.stages = StageRunner(self.model, self.model_stream, self._emit)
        self.subagents = SubagentRunner(
            self.model_stream,
            self.registry,
            self.config,
            self.permission_store,
            self._emit,
        )
        self.graph = MainAgentGraph(self)
        self.summarizer = summarizer
        self._uses_model_summarizer = self.summarizer is None and isinstance(
            model,
            OpenAICompatibleModel,
        )
        if self._uses_model_summarizer:
            self.summarizer = lambda prompt: self.stages.complete(prompt, stage="compact")
        self._run_events: list[dict[str, Any]] = []
        self._session_id: str | None = None
        self._turn_id: str | None = None
        self._run_active = False
        self._steering_lock = Lock()
        self._steering_queues: dict[str, list[dict[str, str]]] = {}
        self._stop_requests: set[str] = set()

    def new_state(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        goal: str | None = None,
    ) -> tuple[AgentState, str]:
        """Create a well-formed state without persisting a session."""
        sid = session_id or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        profile = self.memory_recall.recall("default")
        state = initial_state(
            user_input=user_input,
            session_id=sid,
            goal=goal,
            mode=self.config.mode,
            max_iterations=self.config.max_iterations,
            memory_profile=profile.model_dump(),
        )
        return state, sid

    def invoke(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        goal: str | None | object = _GOAL_UNSET,
        active_skills: list[dict[str, Any]] | None = None,
        restart_goal: bool = False,
    ) -> AgentState:
        """Execute one user turn and persist replayable events."""
        new_session = session_id is None
        resolved_goal = goal if isinstance(goal, str) else None
        goal_restarted = False
        if new_session:
            state, session_id = self.new_state(user_input, goal=resolved_goal)
            self.session_store.create(
                SessionRecord(
                    session_id=session_id,
                    user_id="default",
                    goal=resolved_goal,
                    mode=self.config.mode,
                )
            )
        else:
            state = self.session_store.load_state(str(session_id))
            self._ensure_state_defaults(state)
            if goal is not _GOAL_UNSET:
                goal_restarted = restart_goal or state.get("goal") != resolved_goal
                if goal_restarted:
                    reset_goal_scope(state, resolved_goal)
                else:
                    state["goal"] = resolved_goal
            reset_turn_scope(state)

        if active_skills is not None:
            state["active_skills"] = self._merge_skills(
                state.get("active_skills", []),
                active_skills,
            )

        state["user_input"] = user_input
        state["mode"] = self.config.mode  # type: ignore[typeddict-item]
        state["messages"] = list(state.get("messages", [])) + [
            {"role": "user", "content": user_input}
        ]
        state["pending_confirmation"] = None
        state["pending_user_question"] = None
        state["pending_tool_calls"] = []
        state["finished"] = False
        state["finish_reason"] = None
        state["iteration"] = 0
        state["turn_id"] = datetime.now().strftime("%Y%m%d-%H%M%S-%f")

        self._begin_run(str(session_id), str(state["turn_id"]))
        try:
            self._emit(
                AgentEvent(
                    type="turn.started",
                    payload={
                        "user_input": user_input,
                        "goal": state.get("goal"),
                        "goal_restarted": goal_restarted,
                    },
                )
            )
            result = self._run_react(state)
            result["session_id"] = str(session_id)
            self._finish_turn(result)
            self._save_session(result)
            self.memory_updater.register_turn(user_input, str(result.get("response", "")))
            return result
        except Exception as exc:
            state["errors"] = list(state.get("errors", [])) + [
                {"stage": str(state.get("stage") or "main"), "error": str(exc)}
            ]
            state["finished"] = True
            state["finish_reason"] = "error"
            try:
                self._emit(
                    AgentEvent(
                        type="turn.failed",
                        payload={"error": str(exc), "finish_reason": "error"},
                    )
                )
                self._save_session(state)
            except Exception:
                # 保留原始异常；此前稳定事件已经尽可能逐条写入。
                pass
            raise
        finally:
            self._end_run()

    def _run_react(self, state: AgentState) -> AgentState:
        """由 LangGraph 驱动一个完整 turn。"""
        return self.graph.invoke(state)

    def _graph_main_agent(self, state: AgentState) -> tuple[AgentState, str]:
        """执行一次模型决策，并选择工具、反思或结束节点。"""
        if self._consume_stop_request(state, boundary="before_model"):
            return state, "end"

        iteration = int(state.get("iteration", 0)) + 1
        state["iteration"] = iteration
        if iteration > int(state.get("max_iterations", self.config.max_iterations)):
            state["response"] = state.get("response") or "已达到最大执行轮数。"
            state["finished"] = True
            state["finish_reason"] = "iteration_limit"
            return state, "end"

        state["stage"] = "main"
        context = self._build_context(state)
        if self._maybe_auto_compact(state):
            context = str(state.get("context") or "")
        batch = self._call_model(context, state, stage="main")
        self._add_usage(state, batch.usage)
        self._record_context_usage(state, batch.usage, stage="main")
        if batch.error:
            state["errors"] = list(state.get("errors", [])) + [
                {"stage": "main", "error": batch.error}
            ]
            state["response"] = f"模型调用失败：{batch.error}"
            state["finished"] = True
            state["finish_reason"] = "error"
            return state, "end"

        if self._consume_stop_request(state, boundary="after_model"):
            return state, "end"

        if batch.calls:
            if self._apply_pending_steering(state, boundary="immediate"):
                return state, "main_agent"
            signature = json.dumps(batch.calls, ensure_ascii=False, sort_keys=True)
            if signature == state.get("_last_tool_signature") and state.get("tool_results"):
                state["response"] = self._last_tool_output(state)
                state["streamed_response"] = ""
                state["finished"] = True
                state["finish_reason"] = "repeated_tool_call"
                self._emit(
                    AgentEvent(
                        type="item.completed",
                        stage="main",
                        item_type="message",
                        content=state["response"],
                        payload={"content": state["response"], "streamed": False},
                    )
                )
                return state, "end"
            state["_last_tool_signature"] = signature
            state["tool_calls"] = batch.calls
            state["messages"] = list(state.get("messages", [])) + [
                {
                    "role": "assistant",
                    "content": batch.text,
                    "tool_calls": [dict(call) for call in batch.calls],
                }
            ]
            return state, "tool_batch"

        if batch.text:
            state["messages"] = list(state.get("messages", [])) + [
                {"role": "assistant", "content": batch.text}
            ]

        # 没有工具可作为纠偏边界时，在结束或反思前兜底应用用户输入。
        if self._apply_pending_steering(state, boundary="before_finish"):
            return state, "main_agent"

        state["response"] = batch.text or self._last_tool_output(state)
        state["streamed_response"] = batch.text
        if not state.get("goal"):
            state["finished"] = True
            state["finish_reason"] = "completed"
            return state, "end"
        return state, "reflection"

    def _graph_tool_batch(self, state: AgentState) -> tuple[AgentState, str]:
        """执行一个工具批次；默认在整批工具完成后应用用户纠偏。"""
        calls = list(state.get("tool_calls", []))
        if self._execute_tool_batch(state, calls):
            return state, "end"
        if self._consume_stop_request(state, boundary="after_tool"):
            return state, "end"
        self._apply_pending_steering(state, boundary="after_tool")
        return state, "main_agent"

    def _graph_reflection(self, state: AgentState) -> tuple[AgentState, str]:
        """评估 goal，并将未完成原因反馈给主 Agent。"""
        state["stage"] = "reflect"
        reflection = self.stages.reflect(state)
        self._add_usage(state, self.stages.last_usage)
        state["reflection"] = reflection.model_dump()
        state["reflection_count"] = int(state.get("reflection_count", 0)) + 1

        if self._consume_stop_request(state, boundary="after_reflection"):
            return state, "end"
        if self._apply_pending_steering(state, boundary="before_finish"):
            return state, "main_agent"
        if reflection.complete:
            state["goal_complete"] = True
            state["finished"] = True
            state["finish_reason"] = "goal_complete"
            return state, "end"
        if reflection.needs_user or reflection.blocked:
            state["response"] = reflection.feedback or reflection.summary or state["response"]
            state["finished"] = True
            state["finish_reason"] = "needs_user" if reflection.needs_user else "blocked"
            if reflection.needs_user:
                self._set_pending_user_question(
                    state,
                    {
                        "question": reflection.question or state["response"],
                        "options": reflection.options,
                        "allow_custom": True,
                    },
                )
            else:
                self._emit(
                    AgentEvent(
                        type="item.completed",
                        stage="reflect",
                        item_type="message",
                        payload={"content": state["response"]},
                    )
                )
            return state, "end"
        if int(state["reflection_count"]) >= self.config.max_reflections:
            state["response"] = reflection.feedback or "目标尚未完成，已达到反思重试上限。"
            state["finished"] = True
            state["finish_reason"] = "reflection_limit"
            return state, "end"

        feedback = reflection.feedback or "继续验证目标并补齐缺失条件。"
        state["messages"] = list(state.get("messages", [])) + [
            {"role": "reflection", "content": feedback}
        ]
        return state, "main_agent"

    def _call_model(
        self,
        context: str,
        state: dict[str, Any],
        *,
        stage: str,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> ModelBatch:
        """Call the model through the shared stream consumer."""
        schemas = tool_schemas if tool_schemas is not None else self.registry.tool_schemas(
            self._tool_context(state)
        )
        return self.model_stream.call(
            context,
            state,
            stage=stage,
            tool_schemas=schemas,
        )

    def _execute_tool_batch(self, state: AgentState, calls: list[dict[str, Any]]) -> bool:
        """Execute a model tool batch and return whether user interaction paused it."""
        normalized_calls: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        pending_reasons: list[str] = []
        deferred: list[dict[str, Any]] = []
        runnable_calls: list[dict[str, Any]] = []
        runnable_authorizations: list[ToolAuthorization] = []
        runnable_indexes: list[int] = []
        results: list[ToolResult | None] = [None] * len(calls)
        for index, original_call in enumerate(calls):
            authorization = self.registry.authorize_tool(
                str(original_call.get("name") or ""),
                dict(original_call.get("arguments") or {}),
                self._tool_context(state),
            )
            call = {
                **original_call,
                "arguments": authorization.arguments,
            }
            normalized_calls.append(call)
            if authorization.status == "needs_confirmation":
                pending.append(call)
                if authorization.reason:
                    pending_reasons.append(authorization.reason)
                # 审批是执行顺序屏障，后续调用必须等当前调用处理后再授权和执行。
                deferred = [dict(item) for item in calls[index + 1 :]]
                break
            if not authorization.allowed:
                results[index] = authorization.to_result()
                continue
            runnable_calls.append(call)
            runnable_authorizations.append(authorization)
            runnable_indexes.append(index)

        state["tool_calls"] = [*normalized_calls, *deferred]

        # Registry 只并行无副作用调用，并按原始 call 顺序返回结果。
        completed = self.registry.execute_many(
            runnable_calls,
            lambda: self._tool_context(state),
            max_workers=self.config.max_parallel_tools,
            authorizations=runnable_authorizations,
        )
        for index, result in zip(runnable_indexes, completed):
            results[index] = result

        pending_question: dict[str, Any] | None = None
        for call, result in zip(normalized_calls, results):
            if result is None:
                continue
            self._emit_tool_result(call, result, stage=str(state.get("stage", "main")))
            self._apply_tool_result(state, call, result)
            data = result.data if isinstance(result.data, dict) else {}
            question = data.get("user_question")
            if isinstance(question, dict) and pending_question is None:
                pending_question = question

        state["approved_tool_calls"] = []
        if pending_question is not None:
            self._set_pending_user_question(state, pending_question)
        if pending:
            # 审批是可恢复暂停点，授权状态不能伪装成 tool result。
            state["pending_tool_calls"] = pending
            state["deferred_tool_calls"] = deferred
            request = ApprovalRequest.from_calls(
                pending,
                deferred_calls=deferred,
                reason="; ".join(dict.fromkeys(pending_reasons)),
            )
            state["pending_confirmation"] = request.as_payload()
            self._emit(
                AgentEvent(
                    type="approval.requested",
                    item_type="approval",
                    call_id=request.calls[0].call_id,
                    tool_name=request.tool_name,
                    arguments=request.arguments,
                    payload=request.as_payload(),
                    options=[option.value for option in request.options],
                )
            )
            state["finish_reason"] = "approval_required"
            return True
        state["deferred_tool_calls"] = []
        return pending_question is not None

    def _set_pending_user_question(
        self,
        state: AgentState,
        question: dict[str, Any],
    ) -> None:
        """保存结构化问题，使会话可以在用户回答后继续。"""
        content = str(question.get("question") or question.get("content") or "").strip()
        raw_options = question.get("options") or []
        if not isinstance(raw_options, list):
            raw_options = []
        options = list(
            dict.fromkeys(
                str(item).strip()
                for item in raw_options
                if str(item).strip()
            )
        )
        pending = {
            "question": content,
            "options": options,
            "allow_custom": bool(question.get("allow_custom", True)),
        }
        state["pending_user_question"] = pending
        state["finish_reason"] = "user_input_required"
        self._emit(
            AgentEvent(
                type="item.completed",
                stage=str(state.get("stage", "main")),  # type: ignore[arg-type]
                item_type="user_question",
                content=content,
                payload=pending,
                options=options,
            )
        )

    def _apply_tool_result(
        self,
        state: AgentState,
        call: dict[str, Any],
        result: ToolResult,
    ) -> None:
        dumped = result.model_dump()
        state["tool_results"] = list(state.get("tool_results", [])) + [dumped]
        state["messages"] = list(state.get("messages", [])) + [
            {
                "role": "tool",
                "content": result.to_message(),
                "tool_call_id": str(call.get("call_id") or ""),
                "name": str(call.get("name") or result.tool_name),
            }
        ]
        if result.status in {"error", "blocked"}:
            state["errors"] = list(state.get("errors", [])) + [
                {"tool": call.get("name"), "error": result.output}
            ]
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("plan") is not None:
            state["plan"] = data["plan"]
            state["tasks"] = data.get("tasks", [])
        active_skill = data.get("active_skill")
        if isinstance(active_skill, dict):
            skills = [
                skill
                for skill in state.get("active_skills", [])
                if skill.get("name") != active_skill.get("name")
            ]
            skills.append(active_skill)
            state["active_skills"] = skills
        if isinstance(data.get("usage"), dict):
            self._add_usage(state, data["usage"])

    def resolve_approval(self, session_id: str, decision: ApprovalDecision) -> AgentState:
        """Resolve pending calls, optionally persist rules, then continue the loop."""
        if decision not in VALID_APPROVAL_DECISIONS:
            raise ValueError(f"invalid approval decision: {decision}")
        state = self.session_store.load_state(session_id)
        self._ensure_state_defaults(state)
        pending_payload = dict(state.get("pending_confirmation") or {})
        pending_calls = pending_payload.get("calls") or state.get("pending_tool_calls") or []
        if pending_payload:
            pending_payload["calls"] = pending_calls
            request = ApprovalRequest.model_validate(pending_payload)
        else:
            request = None
        pending = (
            [call.model_dump() for call in request.calls]
            if request is not None
            else list(pending_calls)
        )
        deferred = (
            [call.model_dump() for call in request.deferred_calls]
            if request is not None and request.deferred_calls
            else list(state.get("deferred_tool_calls") or [])
        )
        if not pending:
            raise ValueError("session has no pending approval")
        if request is not None and not request.accepts(decision):
            raise ValueError(f"invalid approval decision: {decision}")
        if decision != "deny":
            normalized_pending: list[dict[str, Any]] = []
            for call in pending:
                normalized = dict(call)
                try:
                    normalized["arguments"] = self.registry.normalize_arguments(
                        str(call.get("name") or ""),
                        dict(call.get("arguments") or {}),
                    )
                except Exception as exc:
                    # 旧审批在恢复时也必须重新通过当前 schema，不能持久化失效授权。
                    raise ValueError(f"pending approval call is invalid: {exc}") from exc
                normalized_pending.append(normalized)
            pending = normalized_pending
        state["turn_id"] = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self._begin_run(session_id, str(state["turn_id"]))
        try:
            self._emit(
                AgentEvent(
                    type="approval.resolved",
                    item_type="approval",
                    payload={"decision": decision, "calls": pending},
                )
            )
            state["pending_tool_calls"] = []
            state["pending_confirmation"] = None
            state["deferred_tool_calls"] = []
            state["tool_results"] = [
                result
                for result in state.get("tool_results", [])
                if result.get("status") != "needs_confirmation"
            ]

            if decision == "deny":
                state["denied_tool_calls"] = list(state.get("denied_tool_calls", [])) + pending
                for call in pending:
                    result = ToolResult(
                        tool_name=str(call["name"]),
                        status="blocked",
                        output="operation denied by user",
                    )
                    self._emit_tool_result(call, result)
                    self._apply_tool_result(state, call, result)
                if deferred and self._execute_tool_batch(state, deferred):
                    self._finish_turn(state)
                    self._save_session(state)
                    return state
            else:
                if decision == "allow_always":
                    # 持久授权只写入仓库级规则，仍受路径和只读模式约束。
                    for call in pending:
                        self.permission_store.allow(
                            str(call["name"]), dict(call.get("arguments") or {})
                        )
                else:
                    state["approved_tool_calls"] = pending
                if self._execute_tool_batch(state, [*pending, *deferred]):
                    self._finish_turn(state)
                    self._save_session(state)
                    return state

            if state.get("pending_user_question"):
                state["finish_reason"] = "user_input_required"
                self._finish_turn(state)
                self._save_session(state)
                return state

            if self._consume_stop_request(state, boundary="after_tool"):
                self._finish_turn(state)
                self._save_session(state)
                return state
            self._apply_pending_steering(state, boundary="after_tool")

            result = self._run_react(state)
            self._finish_turn(result)
            self._save_session(result)
            return result
        finally:
            self._end_run()

    def approve_pending(self, session_id: str) -> AgentState:
        """Compatibility helper for one-time approval."""
        return self.resolve_approval(session_id, "allow_once")

    def deny_pending(self, session_id: str) -> AgentState:
        return self.resolve_approval(session_id, "deny")

    def compact(self, session_id: str, focus: str | None = None) -> AgentState:
        """Manually compact one persisted session."""
        state = self.session_store.load_state(session_id)
        self._ensure_state_defaults(state)
        state["turn_id"] = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self._begin_run(session_id, str(state["turn_id"]))
        try:
            self._compact_state(state, trigger="manual", focus=focus)
            self._save_session(state)
            return state
        finally:
            self._end_run()

    def _maybe_auto_compact(self, state: AgentState) -> bool:
        """Compact before a model request when projected context crosses the threshold."""
        usage = state.get("context_usage") or {}
        projected = int(usage.get("projected_tokens") or usage.get("estimated_tokens") or 0)
        if projected < self.config.compact_threshold_tokens:
            return False
        messages = SessionHistory.from_dicts(state.get("messages", [])).messages
        history_tokens = sum(estimate_message_tokens(message) for message in messages)
        if history_tokens <= self.config.compact_keep_recent_tokens:
            return False
        self._compact_state(state, trigger="auto")
        return True

    def _compact_state(
        self,
        state: AgentState,
        *,
        trigger: str,
        focus: str | None = None,
    ) -> None:
        messages = SessionHistory.from_dicts(state.get("messages", [])).messages
        self._emit(
            AgentEvent(
                type="context.compaction.started",
                stage="compact",
                item_type="compaction",
                payload={
                    "trigger": trigger,
                    "threshold_tokens": self.config.compact_threshold_tokens,
                },
            )
        )
        if self._uses_model_summarizer:
            self.stages.last_usage = {}
        result = self.compactor.compact(messages, self.summarizer, focus=focus)
        if self._uses_model_summarizer:
            self._add_usage(state, self.stages.last_usage)
        state["messages"] = [message.as_dict() for message in result.messages]
        if result.summary:
            state["context_summary"] = result.summary
        # 压缩改变了下一次请求体，旧的 API 实测值不再代表当前上下文。
        state["context_usage"] = {}
        self._build_context(state)
        context_usage = dict(state.get("context_usage") or {})
        self._emit(
            AgentEvent(
                type="context.compaction.completed",
                stage="compact",
                item_type="compaction",
                payload={
                    "trigger": trigger,
                    "summary": state.get("context_summary", ""),
                    "tokens_before": result.tokens_before,
                    "tokens_after": result.tokens_after,
                    "compression_ratio": result.compression_ratio,
                    "context_usage": context_usage,
                },
                progress=1.0,
            )
        )

    def _build_context(self, state: AgentState) -> str:
        context = self.context_builder.build(
            user_input=str(state.get("user_input", "")),
            history=SessionHistory.from_dicts(state.get("messages", [])),
            profile=self.memory_recall.recall(str(state.get("user_id", "default"))),
            tool_schemas=self.registry.tool_schemas(self._tool_context(state)),
            goal=state.get("goal"),
            plan=state.get("plan"),
            tasks=state.get("tasks", []),
            reflection_feedback=self._latest_reflection_feedback(state),
            context_summary=str(state.get("context_summary") or ""),
            skill_index=self.skill_loader.prompt_index(),
            active_skills=state.get("active_skills", []),
        )
        state["context"] = context
        state["_runtime_context"] = self.context_builder.last_runtime_context
        state["_model_messages"] = list(self.context_builder.last_messages)
        estimated = dict(self.context_builder.last_usage)
        previous = dict(state.get("context_usage") or {})
        message_count = len(state.get("messages", []))
        projected = int(estimated.get("used_tokens", 0))
        if previous.get("source") == "response_api":
            anchor = min(message_count, max(0, int(previous.get("message_count", 0))))
            appended = SessionHistory.from_dicts(state.get("messages", [])[anchor:]).messages
            projected = max(
                projected,
                int(previous.get("used_tokens", 0))
                + sum(estimate_message_tokens(message) for message in appended),
            )
        max_tokens = self.config.max_context_tokens
        threshold_tokens = self.config.compact_threshold_tokens
        if previous.get("source") == "response_api":
            used_tokens = int(previous.get("used_tokens", 0))
            source = "response_api"
        else:
            used_tokens = projected
            source = "estimated"
        state["context_usage"] = {
            **previous,
            "used_tokens": used_tokens,
            "estimated_tokens": int(estimated.get("used_tokens", 0)),
            "projected_tokens": projected,
            "max_tokens": max_tokens,
            "remaining_tokens": max(0, max_tokens - used_tokens),
            "percent_used": round(min(1.0, used_tokens / max(1, max_tokens)) * 100, 1),
            "projected_percent": round(
                min(1.0, projected / max(1, max_tokens)) * 100,
                1,
            ),
            "threshold_tokens": threshold_tokens,
            "threshold_percent": round(self.config.compact_threshold * 100, 1),
            "source": source,
        }
        return context

    def _tool_context(self, state: dict[str, Any]) -> ToolContext:
        return ToolContext(
            mode=self.config.normalized_mode,
            allowed_roots=self.config.allowed_roots,
            state=dict(state),
            approved_tool_calls=list(state.get("approved_tool_calls", [])),
            denied_tool_calls=list(state.get("denied_tool_calls", [])),
            permission_store=self.permission_store,
            services={
                "plan_runner": self.stages.run_plan,
                "subagent_runner": self.subagents.run,
                "skill_loader": self.skill_loader,
                "max_tool_output_chars": self.config.max_tool_output_chars,
            },
        )

    def _emit_tool_result(
        self,
        call: dict[str, Any],
        result: ToolResult,
        *,
        stage: str = "main",
    ) -> None:
        self._emit(
            AgentEvent(
                type="item.completed",
                stage=stage,  # type: ignore[arg-type]
                item_type="tool_result",
                call_id=str(call.get("call_id") or ""),
                tool_name=str(call.get("name") or ""),
                arguments=dict(call.get("arguments") or {}),
                result=result.model_dump(),
                payload={"result": result.model_dump()},
            )
        )

    def _emit(self, event: AgentEvent | dict[str, Any]) -> None:
        persistent = event.persistent if isinstance(event, AgentEvent) else (
            event.get("type") != "item.delta" and not event.get("is_delta", False)
        )
        if isinstance(event, AgentEvent):
            if event.session_id is None:
                event.session_id = self._session_id
            if event.turn_id is None:
                event.turn_id = self._turn_id
            data = event.as_event_dict()
        else:
            data = dict(event)
            data.setdefault("session_id", self._session_id)
            data.setdefault("turn_id", self._turn_id)
        self._run_events.append(data)
        if persistent and self._session_id:
            self.session_store.append_events(self._session_id, [data])
        if self.stream_handler:
            self.stream_handler(data)

    def _begin_run(self, session_id: str, turn_id: str) -> None:
        with self._steering_lock:
            self._session_id = session_id
            self._turn_id = turn_id
            self._run_active = True
        self._run_events = []

    def _end_run(self) -> None:
        with self._steering_lock:
            self._run_active = False

    def submit_steering(
        self,
        content: str,
        *,
        session_id: str | None = None,
        delivery: Literal["after_tool", "immediate"] = "after_tool",
    ) -> bool:
        """提交执行中纠偏；默认等下一批工具完成后再交给模型。"""
        message = content.strip()
        with self._steering_lock:
            if not self._run_active:
                return False
            target = session_id or self._session_id
            if session_id is not None and session_id != self._session_id:
                return False
        if not message or not target:
            return False
        with self._steering_lock:
            self._steering_queues.setdefault(target, []).append(
                {"content": message, "delivery": delivery}
            )
        self._emit(
            AgentEvent(
                type="steering.queued",
                item_type="user_message",
                payload={"content": message, "delivery": delivery},
            )
        )
        return True

    def request_stop(self, *, session_id: str | None = None) -> bool:
        """请求在当前模型或工具安全边界停止执行。"""
        with self._steering_lock:
            if not self._run_active:
                return False
            target = session_id or self._session_id
            if session_id is not None and session_id != self._session_id:
                return False
        if not target:
            return False
        with self._steering_lock:
            self._stop_requests.add(target)
        self._emit(
            AgentEvent(
                type="steering.stop_requested",
                item_type="user_message",
                payload={"session_id": target},
            )
        )
        return True

    def _apply_pending_steering(self, state: AgentState, *, boundary: str) -> bool:
        """在 LangGraph 节点边界将排队的用户纠偏写入消息历史。"""
        session_id = str(state.get("session_id") or self._session_id or "")
        if not session_id:
            return False
        with self._steering_lock:
            queued = self._steering_queues.get(session_id, [])
            if boundary == "immediate":
                selected = [item for item in queued if item["delivery"] == "immediate"]
            else:
                selected = list(queued)
            if not selected:
                return False
            selected_ids = {id(item) for item in selected}
            remaining = [item for item in queued if id(item) not in selected_ids]
            if remaining:
                self._steering_queues[session_id] = remaining
            else:
                self._steering_queues.pop(session_id, None)

        additions = [
            {"role": "user", "content": f"执行中用户纠偏：{item['content']}"}
            for item in selected
        ]
        state["messages"] = list(state.get("messages", [])) + additions
        state["steering_history"] = list(state.get("steering_history", [])) + [
            {
                "content": item["content"],
                "delivery": item["delivery"],
                "applied_at": boundary,
            }
            for item in selected
        ]
        # 用户改变方向后，允许模型重新选择与上一轮相同的工具。
        state["_last_tool_signature"] = ""
        for item in selected:
            self._emit(
                AgentEvent(
                    type="steering.applied",
                    item_type="user_message",
                    payload={
                        "content": item["content"],
                        "delivery": item["delivery"],
                        "boundary": boundary,
                    },
                )
            )
        return True

    def _consume_stop_request(self, state: AgentState, *, boundary: str) -> bool:
        session_id = str(state.get("session_id") or self._session_id or "")
        with self._steering_lock:
            requested = session_id in self._stop_requests
            self._stop_requests.discard(session_id)
        if not requested:
            return False
        state["response"] = "已根据用户请求停止当前执行。"
        state["finished"] = True
        state["finish_reason"] = "user_stopped"
        self._emit(
            AgentEvent(
                type="steering.stopped",
                item_type="user_message",
                payload={"boundary": boundary},
            )
        )
        return True

    def _finish_turn(self, state: AgentState) -> None:
        event_type = "turn.failed" if state.get("finish_reason") == "error" else "turn.completed"
        self._emit(
            AgentEvent(
                type=event_type,
                payload={
                    "finish_reason": state.get("finish_reason") or "completed",
                    "goal_complete": bool(state.get("goal_complete")),
                    "usage": state.get("usage", {}),
                    "context_usage": state.get("context_usage", {}),
                },
                usage=state.get("usage"),
            )
        )

    def _save_session(self, state: AgentState) -> None:
        session_id = str(state["session_id"])
        checkpoint = dict(state)
        checkpoint.pop("_last_tool_signature", None)
        checkpoint.pop("_runtime_context", None)
        self.session_store.append_events(
            session_id,
            [
                {
                    "type": "state.checkpoint",
                    "session_id": session_id,
                    "turn_id": state.get("turn_id"),
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "state": checkpoint,
                }
            ],
        )

    def _ensure_state_defaults(self, state: dict[str, Any]) -> None:
        defaults = initial_state(
            user_input=str(state.get("user_input", "")),
            session_id=str(state.get("session_id", "default")),
            goal=state.get("goal"),
            mode=str(state.get("mode", self.config.mode)),
            max_iterations=int(state.get("max_iterations", self.config.max_iterations)),
        )
        for key, value in defaults.items():
            state.setdefault(key, value)
        usage = dict(state.get("context_usage") or {})
        if usage:
            usage["max_tokens"] = self.config.max_context_tokens
            usage["threshold_tokens"] = self.config.compact_threshold_tokens
            usage["threshold_percent"] = round(self.config.compact_threshold * 100, 1)
            used_tokens = int(usage.get("used_tokens", 0))
            usage["remaining_tokens"] = max(0, self.config.max_context_tokens - used_tokens)
            usage["percent_used"] = round(
                min(1.0, used_tokens / max(1, self.config.max_context_tokens)) * 100,
                1,
            )
            projected = int(usage.get("projected_tokens", used_tokens))
            usage["projected_percent"] = round(
                min(1.0, projected / max(1, self.config.max_context_tokens)) * 100,
                1,
            )
            state["context_usage"] = usage

    def _add_usage(self, state: AgentState, usage: dict[str, int]) -> None:
        normalized = normalize_usage(usage)
        if not normalized:
            return
        state["usage"] = accumulate_usage(state.get("usage"), normalized)
        state["last_response_usage"] = normalized

    def _record_context_usage(
        self,
        state: AgentState,
        usage: dict[str, int],
        *,
        stage: str,
    ) -> None:
        """Use provider-reported input tokens as the authoritative context measurement."""
        normalized = normalize_usage(usage)
        if "input_tokens" not in normalized:
            return
        used_tokens = normalized["input_tokens"]
        max_tokens = self.config.max_context_tokens
        previous = dict(state.get("context_usage") or {})
        state["context_usage"] = {
            "used_tokens": used_tokens,
            "estimated_tokens": int(previous.get("estimated_tokens", 0)),
            "projected_tokens": used_tokens,
            "max_tokens": max_tokens,
            "remaining_tokens": max(0, max_tokens - used_tokens),
            "percent_used": round(min(1.0, used_tokens / max(1, max_tokens)) * 100, 1),
            "projected_percent": round(
                min(1.0, used_tokens / max(1, max_tokens)) * 100,
                1,
            ),
            "threshold_tokens": self.config.compact_threshold_tokens,
            "threshold_percent": round(self.config.compact_threshold * 100, 1),
            "source": "response_api",
            "stage": stage,
            "message_count": len(state.get("messages", [])),
        }

    @staticmethod
    def _merge_skills(
        existing: list[dict[str, Any]],
        selected: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged = {str(skill.get("name")): skill for skill in existing if skill.get("name")}
        for skill in selected:
            if skill.get("name"):
                merged[str(skill["name"])] = skill
        return list(merged.values())

    @staticmethod
    def _last_tool_output(state: dict[str, Any]) -> str:
        for result in reversed(state.get("tool_results", [])):
            if result.get("output"):
                return str(result["output"])
        return ""

    @staticmethod
    def _latest_reflection_feedback(state: dict[str, Any]) -> str | None:
        reflection = state.get("reflection") or {}
        return str(reflection.get("feedback") or "") or None

    def set_mode(self, mode: str) -> None:
        self.config.mode = mode

    def configure_context(
        self,
        *,
        max_tokens: int | None = None,
        compact_threshold: float | None = None,
        keep_recent_tokens: int | None = None,
        reset: bool = False,
        persist: bool = True,
    ) -> dict[str, int | float]:
        """Update context limits and keep all runtime components in sync."""
        defaults = RuntimeConfig()
        resolved_max = defaults.max_context_tokens if reset else self.config.max_context_tokens
        resolved_threshold = defaults.compact_threshold if reset else self.config.compact_threshold
        resolved_keep = (
            defaults.compact_keep_recent_tokens
            if reset
            else self.config.compact_keep_recent_tokens
        )
        if max_tokens is not None:
            resolved_max = max_tokens
        if compact_threshold is not None:
            resolved_threshold = compact_threshold
        if keep_recent_tokens is not None:
            resolved_keep = keep_recent_tokens

        threshold_tokens = int(resolved_max * resolved_threshold)
        if keep_recent_tokens is None and resolved_keep >= threshold_tokens:
            resolved_keep = max(1, threshold_tokens // 4)

        RuntimeConfigStore._validate(resolved_max, resolved_threshold, resolved_keep)
        self.config.max_context_tokens = resolved_max
        self.config.compact_threshold = resolved_threshold
        self.config.compact_keep_recent_tokens = resolved_keep
        self.config.compact_reserve_tokens = max(
            1,
            resolved_max - self.config.compact_threshold_tokens,
        )
        self.context_builder.max_tokens = resolved_max
        self.compactor.keep_recent_tokens = resolved_keep
        if persist:
            self.runtime_config_store.save(self.config)
        return {
            "max_context_tokens": resolved_max,
            "compact_threshold": resolved_threshold,
            "compact_threshold_tokens": self.config.compact_threshold_tokens,
            "compact_keep_recent_tokens": resolved_keep,
        }

    def update_model(
        self,
        model_name: str,
        reasoning_effort: str | None = None,
    ) -> OpenAICompatibleModel:
        if not isinstance(self.model, OpenAICompatibleModel):
            raise TypeError("当前模型客户端不支持动态更新")
        effort = reasoning_effort or self.model.reasoning_effort or "none"
        if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("reasoning_effort 必须是 none、low、medium、high、xhigh 或 max")
        self.model = OpenAICompatibleModel(
            api_key=self.model.api_key,
            base_url=self.model.base_url,
            model=model_name,
            reasoning_effort=effort,
        )
        self.model_stream.model = self.model
        self.stages.model = self.model
        if self.model_config:
            self.model_config.model = model_name
            self.model_config.reasoning_effort = effort
            self.model_config_loader.save(self.model_config)
        return self.model

    def list_models(self) -> list[str]:
        """Return models advertised by the configured provider."""
        if not isinstance(self.model, OpenAICompatibleModel):
            raise TypeError("当前模型客户端不支持获取模型列表")
        return self.model.list_models()

    def activate_skill(self, name: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        skill = self.skill_loader.load(name).model_dump()
        if state is not None:
            values = [item for item in state.get("active_skills", []) if item.get("name") != name]
            values.append(skill)
            state["active_skills"] = values
        return skill

    def resume(self, session_id: str) -> AgentState:
        state = self.session_store.load_state(session_id)
        self._ensure_state_defaults(state)
        return state  # type: ignore[return-value]

    def create_session(self, goal: str | None = None) -> SessionRecord:
        """Create an empty resumable session for session-level commands."""
        state, session_id = self.new_state("", goal=goal)
        self.session_store.create(
            SessionRecord(
                session_id=session_id,
                user_id="default",
                goal=goal,
                mode=self.config.mode,
                state=state,
            )
        )
        self.session_store.append_events(
            session_id,
            [
                {
                    "type": "state.checkpoint",
                    "session_id": session_id,
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "state": dict(state),
                }
            ],
        )
        return self.session_store.load(session_id)

    def continue_session(self, session_id: str, user_input: str) -> AgentState:
        return self.invoke(user_input, session_id=session_id)

    def rename_session(self, session_id: str, name: str) -> SessionRecord:
        return self.session_store.rename(session_id, name)

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        return self.session_store.list_sessions(limit)
