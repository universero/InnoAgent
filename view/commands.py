"""Slash command parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Command:
    """Parsed slash-command representation."""
    name: str
    args: list[str]
    raw: str


def parse_command(text: str) -> Command | None:
    """Parse a slash command or return None for natural-language input."""
    if not text.startswith("/"):
        return None
    parts = text.strip().split(maxsplit=1)
    name = parts[0].lstrip("/").lower()
    rest = parts[1] if len(parts) > 1 else ""
    args = rest.split() if rest else []
    return Command(name=name, args=args, raw=text.strip())


def command_info(name: str) -> dict[str, Any]:
    """Return whether a command name is known."""
    return {
        "known": name
        in {
            "help",
            "goal",
            "plan",
            "tools",
            "status",
            "context",
            "mode",
            "model",
            "permissions",
            "skills",
            "skill",
            "tasks",
            "compact",
            "approve",
            "steer",
            "new",
            "resume",
            "sessions",
            "rename",
            "stop",
            "clear",
            "quit",
        }
    }
