"""Session resume helpers for the CLI."""

from __future__ import annotations

from core.runtime.agent import InnoAgentRuntime
from core.session.store import SessionRecord


def pick_session(runtime: InnoAgentRuntime, session_id: str | None) -> SessionRecord | None:
    """Resolve explicit id, or fall back to the most recent session."""
    if session_id:
        try:
            return runtime.session_store.load(session_id)
        except KeyError:
            for record in runtime.list_sessions(limit=100):
                if record.name == session_id:
                    return record
            return None
    return runtime.session_store.latest()


def resume_summary(record: SessionRecord) -> str:
    """Render a one-line summary for a resumed session."""
    state = record.state
    goal = state.get("goal") or "无"
    plan = state.get("plan")
    plan_text = ""
    if plan:
        plan_text = f" plan={plan.get('status', 'active')} rev{plan.get('revision', 1)}"
    label = record.name or record.session_id
    return f"已恢复 {label}：goal={goal}{plan_text}"
