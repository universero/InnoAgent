"""JSONL-backed session store.

Each session is stored as one ``.jsonl`` file.  Every line is a complete event
object, so a conversation can be replayed by reading lines in order.  The final
``state_snapshot`` event carries enough state to resume the session.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class SessionRecord(BaseModel):
    """Lightweight session metadata used by CLI commands."""
    session_id: str
    user_id: str = "default"
    name: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    goal: str | None = None
    mode: str = "auto"
    state: dict[str, Any] = Field(default_factory=dict)


def _now() -> str:
    """Return the current UTC timestamp as ISO text."""
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    """Store one ``.jsonl`` file per session."""

    def __init__(self, root: str | Path = ".innoagent/sessions") -> None:
        """Store the session directory."""
        self.root = Path(root)

    def _path(self, session_id: str) -> Path:
        """Return the JSONL path for a session."""
        return self.root / f"{session_id}.jsonl"

    def create(self, record: SessionRecord) -> None:
        """Write the initial session_meta line."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.session_id)
        if not path.exists():
            path.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "session_id": record.session_id,
                        "user_id": record.user_id,
                        "name": record.name,
                        "created_at": record.created_at.isoformat(),
                        "updated_at": record.updated_at.isoformat(),
                        "goal": record.goal,
                        "mode": record.mode,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )

    def append_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        """Append one or more complete JSON events."""
        if not events:
            return
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(
                    json.dumps(event, ensure_ascii=False, default=str) + "\n"
                )

    def load_events(self, session_id: str) -> list[dict[str, Any]]:
        """Read all replayable events from a session file."""
        path = self._path(session_id)
        if not path.exists():
            raise KeyError(f"session not found: {session_id}")
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def load(self, session_id: str) -> SessionRecord:
        """Reconstruct session metadata and latest state."""
        events = self.load_events(session_id)
        meta: dict[str, Any] | None = None
        name: str | None = None
        updated_at = datetime.now(timezone.utc)
        for event in events:
            event_type = event.get("type")
            if event_type == "session_meta":
                meta = event
                name = event.get("name")
            elif event_type == "session_rename":
                name = event.get("name")
            if event.get("timestamp"):
                updated_at = _parse_time(event.get("timestamp"))
        if meta is None:
            raise KeyError(f"session not found: {session_id}")
        return SessionRecord(
            session_id=str(meta.get("session_id", session_id)),
            user_id=str(meta.get("user_id", "default")),
            name=name,
            created_at=_parse_time(meta.get("created_at")),
            updated_at=updated_at,
            goal=meta.get("goal"),
            mode=str(meta.get("mode", "auto")),
            state=_reconstruct_state(events, meta),
        )

    def load_state(self, session_id: str) -> dict[str, Any]:
        """Return reconstructed state for a session."""
        return self.load(session_id).state

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        """List recent sessions ordered by file modification time."""
        if not self.root.exists():
            return []
        records: list[SessionRecord] = []
        for path in sorted(self.root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            session_id = path.stem
            try:
                records.append(self.load(session_id))
            except KeyError:
                continue
            if len(records) >= limit:
                break
        return records

    def latest(self) -> SessionRecord | None:
        """Return the most recently modified session."""
        records = self.list_sessions(limit=1)
        return records[0] if records else None

    def rename(self, session_id: str, name: str) -> SessionRecord:
        """Append a session_rename event and return the updated record."""
        self.append_events(
            session_id,
            [{"type": "session_rename", "name": name.strip() or None, "timestamp": _now()}],
        )
        return self.load(session_id)


def _parse_time(value: Any) -> datetime:
    """Parse ISO timestamps into timezone-aware datetime objects."""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return datetime.now(timezone.utc)


def _reconstruct_state(events: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a minimal AgentState from the replayable event log."""
    state: dict[str, Any] = {
        "session_id": str(meta.get("session_id", "")),
        "user_id": str(meta.get("user_id", "default")),
        "user_input": "",
        "messages": [],
        "next_action": "continue",
        "finished": False,
        "response": "",
        "streamed_response": "",
        "tool_calls": [],
        "tool_results": [],
        "goal": meta.get("goal"),
        "goal_complete": False,
        "reflection": None,
        "reflection_count": 0,
        "plan": None,
        "tasks": [],
        "plan_mode": False,
        "memory_profile": {},
        "memory_updated": False,
        "mode": str(meta.get("mode", "auto")),
        "approved_tool_calls": [],
        "iteration": 0,
        "max_iterations": 20,
        "errors": [],
        "pending_confirmation": None,
    }

    assistant_text = ""
    for event in events:
        event_type = event.get("type")
        if event_type == "user_input":
            if assistant_text:
                state["messages"].append({"role": "assistant", "content": assistant_text})
                assistant_text = ""
            content = str(event.get("user_input", ""))
            state["user_input"] = content
            state["messages"].append({"role": "user", "content": content})
        elif event_type == "text.delta":
            assistant_text += str(event.get("content", ""))
        elif event_type == "reasoning.delta":
            continue
        elif event_type == "text":
            if assistant_text:
                state["messages"].append({"role": "assistant", "content": assistant_text})
                assistant_text = ""
            content = str(event.get("content", ""))
            if content:
                state["messages"].append({"role": "assistant", "content": content})
            if event.get("kind") == "final":
                state["response"] = content
                state["streamed_response"] = content
            elif event.get("kind") == "plan":
                state["plan"] = event.get("plan")
                state["tasks"] = event.get("tasks") or []
            elif event.get("kind") == "reflection":
                state["reflection"] = {
                    "feedback": content,
                    "complete": bool(event.get("goal_complete")),
                }
                state["goal_complete"] = bool(event.get("goal_complete"))
        elif event_type == "tool" and event.get("phase") == "result":
            if assistant_text:
                state["messages"].append({"role": "assistant", "content": assistant_text})
                assistant_text = ""
            result = {
                "tool_name": event.get("tool_name"),
                "status": event.get("status"),
                "output": event.get("summary", ""),
                "data": None,
                "warnings": event.get("warnings") or [],
                "metadata": {"arguments": event.get("arguments") or {}},
            }
            state["tool_results"].append(result)
            state["messages"].append(
                {
                    "role": "tool",
                    "content": result["output"] or result["status"] or "",
                }
            )
        elif event_type == "needs_confirmation":
            state["pending_confirmation"] = {
                "tool_name": event.get("tool_name"),
                "status": "needs_confirmation",
                "output": event.get("output", ""),
                "metadata": {"arguments": event.get("arguments") or {}},
            }
        elif event_type == "finish":
            if assistant_text:
                state["messages"].append({"role": "assistant", "content": assistant_text})
                state["response"] = assistant_text
                state["streamed_response"] = assistant_text
                assistant_text = ""
            state["finished"] = True
    return state
