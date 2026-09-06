"""Main LangGraph assembly."""

from __future__ import annotations

from core.compat import silence_langgraph_deprecations

silence_langgraph_deprecations()

from langgraph.graph import END, START, StateGraph

from core.runtime.nodes import RuntimeDependencies, RuntimeNodes, route_after_continue, route_after_execute, route_after_goal_check
from core.runtime.state import AgentState


def build_main_graph(deps: RuntimeDependencies):
    """Build and compile the InnoAgent main graph."""
    nodes = RuntimeNodes(deps)
    graph = StateGraph(AgentState)

    graph.add_node("execute", nodes.execute)
    graph.add_node("tool_use", nodes.tool_use)
    graph.add_node("planning", nodes.planning)
    graph.add_node("reflection", nodes.reflection)
    graph.add_node("continue_execute", nodes.continue_execute)
    graph.add_node("goal_check", nodes.goal_check)

    graph.add_edge(START, "execute")

    graph.add_conditional_edges(
        "execute",
        route_after_execute,
        {
            "tool_use": "tool_use",
            "planning": "planning",
            "reflection": "reflection",
            "goal_check": "goal_check",
            "continue_execute": "continue_execute",
        },
    )
    graph.add_edge("tool_use", "continue_execute")
    graph.add_edge("planning", "continue_execute")
    graph.add_edge("reflection", "continue_execute")

    graph.add_conditional_edges(
        "continue_execute",
        route_after_continue,
        {
            "execute": "execute",
            "goal_check": "goal_check",
        },
    )
    graph.add_conditional_edges(
        "goal_check",
        route_after_goal_check,
        {
            "end": END,
            "execute": "execute",
        },
    )
    return graph.compile()
