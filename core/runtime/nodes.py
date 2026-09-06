"""Runtime graph nodes.

The nodes are intentionally small wrappers around the services.  All policy
logic belongs in the service modules, while this file only coordinates state
updates and routing metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from core.memory.recall import MemoryRecall
from core.memory.update import MemoryUpdater
from core.planning.graph import planning_graph
from core.reflection.graph import reflection_graph
from core.runtime.config import RuntimeConfig
from core.runtime.events import make_event
from core.llm import BaseModelClient
from core.runtime.state import AgentState, initial_state
from core.session.context import ContextBuilder
from core.session.history import SessionHistory
from core.tool.base import ToolContext
from core.tool.registry import ToolRegistry
from observe.traces import TraceStore
from observe.metrics import RunMetrics


@dataclass
class RuntimeDependencies:
    """Services and callbacks shared by all runtime nodes."""
    config: RuntimeConfig
    model: BaseModelClient
    registry: ToolRegistry
    context_builder: ContextBuilder
    memory_recall: MemoryRecall
    memory_updater: MemoryUpdater
    tracer: TraceStore | None = None
    metrics: RunMetrics | None = None
    event_handler: Callable[[dict[str, Any]], None] | None = None


class RuntimeNodes:
    """LangGraph node implementations for the main agent loop."""

    def __init__(self, deps: RuntimeDependencies) -> None:
        """Store dependencies for node callbacks."""
        self.deps = deps

    def _trace(self, event: str, data: dict[str, Any]) -> None:
        """Record observability data without affecting execution."""
        if self.deps.tracer:
            self.deps.tracer.record(event, data)
        if self.deps.metrics:
            self.deps.metrics.record_event(event, data)

    def _emit(self, event_type: str, **data: Any) -> None:
        """Emit one structured runtime event."""
        if self.deps.event_handler:
            self.deps.event_handler(make_event(event_type, **data))

    def _tool_context(self, state: AgentState) -> ToolContext:
        """Build the context passed to tools and guardrails."""
        return ToolContext(
            mode=self.deps.config.normalized_mode,
            allowed_roots=self.deps.config.allowed_roots,
            state=dict(state),
            approved_tool_calls=state.get("approved_tool_calls", []),
        )

    def _has_completed_tool_call(
        self, state: AgentState, name: str, arguments: dict[str, Any]
    ) -> bool:
        """Return whether this exact tool call already succeeded this turn."""
        current_turn = state.get("turn_id")
        return any(
            item.get("tool_name") == name
            and item.get("metadata", {}).get("turn_id") == current_turn
            and item.get("metadata", {}).get("arguments") == arguments
            and item.get("status") == "success"
            for item in state.get("tool_results", [])
        )

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Assemble context and ask the model for the next action."""
        profile = self.deps.memory_recall.recall(str(state.get("user_id", "default")))
        if not state.get("memory_profile"):
            state["memory_profile"] = profile.model_dump()

        history = SessionHistory.from_dicts(state.get("messages", []))
        reflection = state.get("reflection") or {}
        reflection_feedback = None
        if reflection and not state.get("goal_complete"):
            reflection_feedback = reflection.get("feedback") or reflection.get("summary")

        tool_context = self._tool_context(state)
        tool_schemas = self.deps.registry.tool_schemas(tool_context)
        context = self.deps.context_builder.build(
            user_input=str(state.get("user_input", "")),
            history=history,
            profile=profile,
            tool_schemas=tool_schemas,
            plan=state.get("plan"),
            tasks=state.get("tasks", []),
            reflection_feedback=reflection_feedback,
        )
        def handle_token(token: str) -> None:
            """将模型文本增量向上传递为 text.delta 事件。"""
            self._emit("text.delta", content=token)

        def handle_thinking(token: str) -> None:
            """将思维链增量向上传递为 reasoning.delta 事件。"""
            self._emit("reasoning.delta", content=token)

        decision = self.deps.model.respond(
            context,
            tool_schemas,
            dict(state),
            on_token=handle_token,
            on_thinking=handle_thinking,
        )
        self._trace(
            "execute",
            {
                "iteration": state.get("iteration", 0),
                "action": decision.action,
                "tool_calls": [call.name for call in decision.tool_calls],
            },
        )
        return {
            "context": context,
            "response": decision.message,
            "streamed_response": decision.message,
            "next_action": decision.action,
            "tool_calls": [call.model_dump() for call in decision.tool_calls],
            "memory_profile": profile.model_dump(),
        }

    def tool_use(self, state: AgentState) -> dict[str, Any]:
        """Execute requested tools and emit structured tool events."""
        tool_context = self._tool_context(state)
        results: list[dict[str, Any]] = []
        updated_plan = state.get("plan")
        updated_tasks = state.get("tasks", [])
        pending_confirmation = None
        messages: list[dict[str, Any]] = []
        current_turn = state.get("turn_id")

        for call in state.get("tool_calls", []):
            name = call.get("name", "")
            arguments = call.get("arguments") or {}
            if self._has_completed_tool_call(state, name, arguments):
                return {
                    "tool_results": [],
                    "messages": [],
                    "plan": updated_plan,
                    "tasks": updated_tasks,
                    "pending_confirmation": None,
                    "approved_tool_calls": [],
                    "response": next(
                        item.get("output", "")
                        for item in reversed(state.get("tool_results", []))
                        if item.get("tool_name") == name
                        and item.get("metadata", {}).get("turn_id") == current_turn
                        and item.get("metadata", {}).get("arguments") == arguments
                        and item.get("status") == "success"
                    ),
                    "next_action": "finish",
                }

            result = self.deps.registry.execute_tool(name, arguments, tool_context)
            result.metadata["turn_id"] = current_turn
            result.metadata["arguments"] = arguments
            argument_text = ", ".join(
                f"{key}={value}" for key, value in arguments.items()
            )
            summary = result.output.strip()
            if len(summary) > 4000:
                summary = summary[:4000] + "..."
            self._trace(
                "tool_use",
                {
                    "tool_name": name,
                    "function": f"{name}({argument_text})" if argument_text else f"{name}()",
                    "arguments": arguments,
                    "status": result.status,
                    "summary": summary,
                    "warnings": result.warnings,
                },
            )
            self._emit(
                "tool",
                phase="result",
                tool_name=name,
                function=f"{name}({argument_text})" if argument_text else f"{name}()",
                arguments=arguments,
                status=result.status,
                summary=summary,
                warnings=result.warnings,
            )
            results.append(result.model_dump())

            if result.status == "needs_confirmation":
                pending_confirmation = result.model_dump()
                messages.append({"role": "assistant", "content": result.output})
                self._emit(
                    "needs_confirmation",
                    tool_name=name,
                    arguments=arguments,
                    output=result.output,
                )
                break
            messages.append({"role": "tool", "content": result.to_message()})

            data = result.data or {}
            if data.get("plan"):
                updated_plan = data["plan"]
            if data.get("tasks"):
                updated_tasks = data["tasks"]

        return {
            "tool_results": results,
            "messages": messages,
            "plan": updated_plan,
            "tasks": updated_tasks,
            "pending_confirmation": pending_confirmation,
            "approved_tool_calls": [],
            "next_action": "finish" if pending_confirmation else "continue",
        }

    def planning(self, state: AgentState) -> dict[str, Any]:
        """Run the planning subgraph and publish its output."""
        goal = state.get("goal") or state.get("user_input", "")
        reflection = state.get("reflection") or {}
        feedback = reflection.get("feedback") if not state.get("goal_complete") else None
        output = planning_graph.invoke(
            {
                "goal": goal,
                "feedback": feedback,
                "existing_plan": state.get("plan"),
            }
        )
        self._trace("planning", {"goal": goal, "revision": output.get("plan", {}).get("revision")})
        self._emit(
            "text",
            kind="plan",
            role="assistant",
            content=output.get("message", "计划已更新"),
            plan=output.get("plan"),
            tasks=output.get("tasks", []),
        )
        return {
            "plan": output.get("plan"),
            "tasks": output.get("tasks", []),
            "messages": [{"role": "assistant", "content": output.get("message", "计划已更新")}],
            "plan_mode": True,
            "response": output.get("message", "计划已更新"),
            "next_action": "continue",
        }

    def reflection(self, state: AgentState) -> dict[str, Any]:
        """Evaluate goal completion and prepare feedback."""
        output = reflection_graph.invoke(
            {
                "goal": state.get("goal") or "",
                "response": state.get("response", ""),
                "plan": state.get("plan"),
                "tasks": state.get("tasks", []),
                "tool_results": state.get("tool_results", []),
                "errors": state.get("errors", []),
            }
        )
        reflection = output.get("reflection") or {}
        complete = bool(output.get("goal_complete", False))
        feedback_message = output.get("feedback", "Reflection completed.")
        self._trace("reflection", {"complete": complete})
        self._emit(
            "text",
            kind="reflection",
            role="assistant",
            content=feedback_message,
            goal_complete=complete,
        )
        return {
            "reflection": reflection,
            "messages": [{"role": "assistant", "content": feedback_message}],
            "goal_complete": complete,
            "reflection_count": int(state.get("reflection_count", 0)) + 1,
            "next_action": "finish" if complete else "continue",
        }

    def continue_execute(self, state: AgentState) -> dict[str, Any]:
        """Advance the loop counter and decide whether to continue."""
        iteration = int(state.get("iteration", 0)) + 1
        next_action = state.get("next_action", "continue")
        messages: list[dict[str, Any]] = []

        if iteration >= int(state.get("max_iterations", 20)):
            messages.append(
                {
                    "role": "system",
                    "content": "达到最大迭代次数，任务停止。",
                }
            )
            return {
                "messages": messages,
                "iteration": iteration,
                "next_action": "finish",
                "finished": False,
                "response": state.get("response", "") or "达到最大迭代次数。",
            }

        return {
            "messages": messages,
            "iteration": iteration,
            "next_action": next_action,
        }

    def goal_check(self, state: AgentState) -> dict[str, Any]:
        """Finalize a run after the model has finished."""
        complete = bool(state.get("goal_complete")) or not state.get("goal")
        return {
            "finished": True,
            "goal_complete": complete,
            "next_action": "end",
            "response": state.get("response", ""),
        }


def route_after_execute(state: AgentState) -> str:
    """Route from the model decision to the matching node."""
    action = state.get("next_action", "finish")
    if action == "tool_use":
        return "tool_use"
    if action == "planning":
        return "planning"
    if action == "reflection":
        return "reflection"
    if action == "finish":
        if state.get("goal") and not state.get("goal_complete"):
            return "reflection"
        return "goal_check"
    return "continue_execute"


def route_after_continue(state: AgentState) -> str:
    """Return to execute or move to goal check."""
    if state.get("next_action") == "finish":
        return "goal_check"
    return "execute"


def route_after_goal_check(state: AgentState) -> str:
    """End the graph or loop back for unfinished goals."""
    if state.get("goal_complete") or not state.get("goal"):
        return "end"
    return "execute"
