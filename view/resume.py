"""Session resume helpers for the CLI."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from core.runtime.agent import InnoAgentRuntime
from core.session.store import SessionRecord


SESSION_PREVIEW_CHARS = 60
SESSION_REPLAY_TURNS = 20
SESSION_REPLAY_EVENTS = 300

_REPLAYABLE_EVENT_TYPES = {
    "approval.resolved",
    "context.compaction.completed",
    "steering.applied",
    "steering.queued",
    "steering.stop_requested",
    "steering.stopped",
    "turn.failed",
    "turn.started",
}


def pick_session(runtime: InnoAgentRuntime, session_id: str | None) -> SessionRecord | None:
    """Resolve an id/name/prefix, or fall back to the most recent session."""
    if session_id:
        try:
            return runtime.session_store.load(session_id)
        except KeyError:
            records = runtime.list_sessions(limit=100)
            query = session_id.casefold()
            for record in records:
                if (record.name or "").casefold() == query:
                    return record
            matches = [
                record
                for record in records
                if record.session_id.casefold().startswith(query)
                or (record.name or "").casefold().startswith(query)
            ]
            if len(matches) == 1:
                return matches[0]
            return None
    return runtime.session_store.latest()


def recent_user_input(
    record: SessionRecord,
    limit: int = SESSION_PREVIEW_CHARS,
) -> str:
    """Return a compact preview of the latest real user turn."""
    state = record.state or {}
    content = str(state.get("user_input") or "")
    if not content:
        for message in reversed(state.get("messages") or []):
            if message.get("role") == "user" and message.get("content"):
                content = str(message["content"])
                break
    compact = " ".join(content.split()) or "empty session"
    if limit > 1 and len(compact) > limit:
        return compact[: limit - 1].rstrip() + "…"
    return compact


def session_choice_description(record: SessionRecord) -> str:
    """Render non-redundant metadata for a session candidate."""
    updated = record.updated_at.astimezone().strftime("%m-%d %H:%M")
    return f"{updated} · {recent_user_input(record)}"


def replayable_session_events(
    events: list[dict[str, Any]],
    *,
    max_turns: int = SESSION_REPLAY_TURNS,
    max_events: int = SESSION_REPLAY_EVENTS,
) -> list[dict[str, Any]]:
    """Select recent user-visible events and normalize them for TUI replay."""
    visible = [
        event
        for event in events
        if event.get("type") == "item.completed"
        or event.get("type") in _REPLAYABLE_EVENT_TYPES
    ]

    turn_starts = [
        index for index, event in enumerate(visible) if event.get("type") == "turn.started"
    ]
    if max_turns > 0 and len(turn_starts) > max_turns:
        visible = visible[turn_starts[-max_turns] :]

    if max_events > 0 and len(visible) > max_events:
        cutoff = len(visible) - max_events
        # 尽量从完整 turn 开始回放，避免首屏只有孤立的工具结果。
        next_turn = next(
            (
                index
                for index in range(cutoff, len(visible))
                if visible[index].get("type") == "turn.started"
            ),
            cutoff,
        )
        visible = visible[next_turn:]

    replay: list[dict[str, Any]] = []
    for event in visible:
        normalized = deepcopy(event)
        if (
            normalized.get("type") == "item.completed"
            and normalized.get("item_type") in {"message", "reasoning"}
        ):
            # Delta 不持久化；回放 completed 事件时必须重新显示完整正文。
            payload = dict(normalized.get("payload") or {})
            payload["streamed"] = False
            normalized["payload"] = payload
        replay.append(normalized)
    return replay


def resume_summary(record: SessionRecord) -> str:
    """Render a one-line summary for a resumed session."""
    label = record.name or record.session_id
    return f"已恢复 {label} · 最近输入：{recent_user_input(record)}"
