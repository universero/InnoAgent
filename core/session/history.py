"""In-memory session history."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Message(BaseModel):
    """A single persisted chat message."""
    role: str
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, str]:
        """Return the OpenAI-compatible message shape."""
        return {"role": self.role, "content": self.content}


class SessionHistory:
    """Small append-only history wrapper."""

    def __init__(self, messages: list[Message] | None = None) -> None:
        """Initialize with an optional existing message list."""
        self.messages = messages or []

    def add(self, role: str, content: str) -> Message:
        """Append a message."""
        message = Message(role=role, content=content)
        self.messages.append(message)
        return message

    def tail(self, limit: int = 20) -> list[Message]:
        """Return the most recent messages."""
        return self.messages[-limit:]

    def clear(self) -> None:
        """Remove all messages."""
        self.messages.clear()

    def to_openai_messages(self, system_prompt: str | None = None) -> list[dict[str, str]]:
        """Convert history into an OpenAI-style message list."""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(message.as_dict() for message in self.tail())
        return messages

    @classmethod
    def from_dicts(cls, messages: list[dict[str, Any]]) -> "SessionHistory":
        """Build history from persisted message dicts."""
        return cls(
            [
                Message(role=str(item.get("role", "user")), content=str(item.get("content", "")))
                for item in messages
            ]
        )

    def to_dicts(self) -> list[dict[str, Any]]:
        """Serialize all messages to plain dicts."""
        return [message.model_dump() for message in self.messages]
