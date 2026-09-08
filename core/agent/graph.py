"""LangGraph control plane for the main agent loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from core.compat import silence_langgraph_deprecations
from core.runtime.state import AgentState

silence_langgraph_deprecations()

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402

if TYPE_CHECKING:
    from core.agent.react import InnoAgent


class MainAgentGraph:
    """Build the single executable graph used by the runtime."""

    def __init__(self, runtime: "InnoAgent") -> None:
        self.runtime = runtime
        self.checkpointer = InMemorySaver()
        self.compiled = self._build()

    def _build(self):
        builder = StateGraph(AgentState)
        builder.add_node("main_agent", self._main_agent)
        builder.add_node("tool_batch", self._tool_batch)
        builder.add_node("reflection", self._reflection)
        builder.add_edge(START, "main_agent")
        return builder.compile(
            checkpointer=self.checkpointer,
            name="innoagent-main",
        )

    def invoke(self, state: AgentState) -> AgentState:
        """Run or resume one turn with the session id as LangGraph thread id."""
        session_id = str(state.get("session_id") or "default")
        # 每轮最多经过 main/tool/reflection 三类节点，额外余量用于安全结束分支。
        limit = max(10, int(state.get("max_iterations", 20)) * 3 + 5)
        result = self.compiled.invoke(
            state,
            config={
                "configurable": {"thread_id": session_id},
                "recursion_limit": limit,
            },
        )
        return result  # type: ignore[return-value]

    def _main_agent(
        self,
        state: AgentState,
    ) -> Command[Literal["main_agent", "tool_batch", "reflection", "__end__"]]:
        # Command 同时提交状态与下一跳，避免在图外维护第二套路由循环。
        update, route = self.runtime._graph_main_agent(dict(state))
        return Command(update=update, goto=self._destination(route))

    def _tool_batch(
        self,
        state: AgentState,
    ) -> Command[Literal["main_agent", "__end__"]]:
        update, route = self.runtime._graph_tool_batch(dict(state))
        return Command(update=update, goto=self._destination(route))

    def _reflection(
        self,
        state: AgentState,
    ) -> Command[Literal["main_agent", "__end__"]]:
        update, route = self.runtime._graph_reflection(dict(state))
        return Command(update=update, goto=self._destination(route))

    @staticmethod
    def _destination(route: str) -> str:
        return END if route == "end" else route
