"""State shared by the event-driven agent runtime and session store."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


NextAction = Literal["continue", "tool_use", "planning", "reflection", "finish", "end"]


class AgentState(TypedDict, total=False):
    """Complete resumable state for one agent session."""
    session_id: str
    turn_id: str
    user_id: str
    user_input: str
    context: str
    messages: list[dict[str, Any]]

    next_action: NextAction
    finished: bool
    response: str
    streamed_response: str
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]

    goal: str | None
    goal_complete: bool
    reflection: dict[str, Any] | None
    reflection_count: int

    plan: dict[str, Any] | None
    tasks: list[dict[str, Any]]
    plan_mode: bool

    memory_profile: dict[str, Any]
    memory_updated: bool
    mode: Literal["auto", "ask", "confirm", "readonly"]
    approved_tool_calls: list[dict[str, Any]]
    denied_tool_calls: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]

    active_skills: list[dict[str, Any]]
    steering_history: list[dict[str, Any]]
    context_summary: str
    context_usage: dict[str, Any]
    usage: dict[str, int]
    stage: str
    finish_reason: str | None
    _last_tool_signature: str

    iteration: int
    max_iterations: int
    errors: list[dict[str, Any]]
    pending_confirmation: dict[str, Any] | None
    pending_user_question: dict[str, Any] | None


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
        denied_tool_calls=[],
        pending_tool_calls=[],
        active_skills=[],
        steering_history=[],
        context_summary="",
        context_usage={},
        usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        stage="main",
        finish_reason=None,
        _last_tool_signature="",
        iteration=0,
        max_iterations=max_iterations,
        errors=[],
        pending_confirmation=None,
        pending_user_question=None,
    )
