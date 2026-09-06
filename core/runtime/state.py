"""LangGraph state definition.

``tool_results``, ``messages`` and ``errors`` use additive reducers so they
accumulate across nodes.  Scalar fields such as ``next_action`` and
``response`` are replaced by the latest node that writes them.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict


NextAction = Literal["continue", "tool_use", "planning", "reflection", "finish", "end"]


class AgentState(TypedDict, total=False):
    """Complete state passed through the LangGraph main loop."""
    session_id: str
    turn_id: str
    user_id: str
    user_input: str
    context: str
    messages: Annotated[list[dict[str, Any]], operator.add]

    next_action: NextAction
    finished: bool
    response: str
    streamed_response: str
    tool_calls: list[dict[str, Any]]
    tool_results: Annotated[list[dict[str, Any]], operator.add]

    goal: str | None
    goal_complete: bool
    reflection: dict[str, Any] | None
    reflection_count: int

    plan: dict[str, Any] | None
    tasks: list[dict[str, Any]]
    plan_mode: bool

    memory_profile: dict[str, Any]
    memory_updated: bool
    mode: Literal["auto", "confirm", "readonly"]
    approved_tool_calls: list[dict[str, Any]]

    iteration: int
    max_iterations: int
    errors: Annotated[list[dict[str, Any]], operator.add]
    pending_confirmation: dict[str, Any] | None


def initial_state(
    *,
    user_input: str,
    session_id: str = "default",
    user_id: str = "default",
    goal: str | None = None,
    mode: str = "auto",
    max_iterations: int = 20,
    memory_profile: dict[str, Any] | None = None,
) -> AgentState:
    """Return a well-formed initial state for a new invocation."""
    return AgentState(
        session_id=session_id,
        turn_id="",
        user_id=user_id,
        user_input=user_input,
        messages=[],
        next_action="continue",
        finished=False,
        response="",
        streamed_response="",
        tool_calls=[],
        tool_results=[],
        goal=goal,
        goal_complete=False,
        reflection=None,
        reflection_count=0,
        plan=None,
        tasks=[],
        plan_mode=False,
        memory_profile=memory_profile or {},
        memory_updated=False,
        mode=mode,  # type: ignore[arg-type]
        approved_tool_calls=[],
        iteration=0,
        max_iterations=max_iterations,
        errors=[],
        pending_confirmation=None,
    )
