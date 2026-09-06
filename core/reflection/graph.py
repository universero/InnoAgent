"""LangGraph subgraph for Reflection."""

from __future__ import annotations

from typing import TypedDict

from core.compat import silence_langgraph_deprecations

silence_langgraph_deprecations()

from langgraph.graph import END, START, StateGraph

from core.reflection.evaluator import evaluate_goal
from core.reflection.feedback import feedback_to_message
from core.reflection.schemas import ReflectionInput, ReflectionResult


class ReflectionGraphState(TypedDict, total=False):
    """State passed through the reflection subgraph."""
    goal: str
    response: str
    plan: dict | None
    tasks: list[dict]
    tool_results: list[dict]
    errors: list[dict]
    reflection: dict | None
    goal_complete: bool
    feedback: str


def _evaluate(state: ReflectionGraphState) -> dict:
    """Evaluate goal completion and produce feedback."""
    input_data = ReflectionInput(
        goal=state["goal"],
        response=state.get("response", ""),
        plan=state.get("plan"),
        tasks=state.get("tasks", []),
        tool_results=state.get("tool_results", []),
        errors=state.get("errors", []),
    )
    result = evaluate_goal(input_data)
    return {
        "reflection": result.model_dump(),
        "goal_complete": result.complete,
        "feedback": feedback_to_message(result),
    }


def build_reflection_graph() -> StateGraph:
    """Compile the Reflection LangGraph subgraph."""
    graph = StateGraph(ReflectionGraphState)
    graph.add_node("evaluate", _evaluate)
    graph.add_edge(START, "evaluate")
    graph.add_edge("evaluate", END)
    return graph.compile()


reflection_graph = build_reflection_graph()
