"""LangGraph subgraph for Planning."""

from __future__ import annotations

from typing import Annotated, TypedDict

from core.compat import silence_langgraph_deprecations

silence_langgraph_deprecations()

from langgraph.graph import END, START, StateGraph

from core.planning.planner import PlanningService
from core.planning.schemas import Plan, PlanningRequest, Task


class PlanningGraphState(TypedDict, total=False):
    """State passed through the planning subgraph."""
    goal: str
    feedback: str | None
    existing_plan: dict | None
    plan: dict | None
    tasks: list[dict]
    message: str


def _load(state: PlanningGraphState) -> dict:
    """Normalize planning state before graph execution."""
    existing = state.get("existing_plan")
    return {
        "goal": state["goal"],
        "feedback": state.get("feedback"),
        "existing_plan": Plan.model_validate(existing) if existing else None,
    }


def _plan(state: PlanningGraphState) -> dict:
    """Create or update the active plan."""
    request = PlanningRequest(
        goal=state["goal"],
        feedback=state.get("feedback"),
        existing_plan=state.get("existing_plan"),  # type: ignore[arg-type]
    )
    output = (
        PlanningService().create_plan(request)
        if request.existing_plan is None
        else PlanningService().update_plan(request)
    )
    return {
        "plan": output.plan.model_dump(),
        "tasks": [task.model_dump() for task in output.tasks],
        "message": output.message,
    }


def _publish(state: PlanningGraphState) -> dict:
    """Publish normalized plan output."""
    return {
        "plan": state.get("plan"),
        "tasks": state.get("tasks", []),
        "message": state.get("message", ""),
    }


def build_planning_graph() -> StateGraph:
    """Compile the Planning LangGraph subgraph."""
    graph = StateGraph(PlanningGraphState)
    graph.add_node("load", _load)
    graph.add_node("plan", _plan)
    graph.add_node("publish", _publish)
    graph.add_edge(START, "load")
    graph.add_edge("load", "plan")
    graph.add_edge("plan", "publish")
    graph.add_edge("publish", END)
    return graph.compile()


planning_graph = build_planning_graph()
