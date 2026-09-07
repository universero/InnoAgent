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


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Slash command metadata shared by help and completion UI."""

    name: str
    usage: str
    description: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandChoice:
    """One static or runtime-provided slash-command argument."""

    value: str
    display: str = ""
    description: str = ""


COMMAND_SPECS = (
    CommandSpec("help", "/help", "show available commands and shortcuts"),
    CommandSpec("status", "/status", "show session, model, goal, and usage"),
    CommandSpec(
        "context",
        "/context [max|threshold|keep|reset] [value]",
        "show or configure context and compaction limits",
        ("max", "threshold", "keep", "reset"),
    ),
    CommandSpec(
        "goal",
        "/goal <text|off>",
        "set and run a goal, or clear it with off",
        ("off", "clear"),
    ),
    CommandSpec("plan", "/plan", "show the current plan"),
    CommandSpec("tasks", "/tasks", "show task progress"),
    CommandSpec("compact", "/compact [focus]", "compact the current session context"),
    CommandSpec("permissions", "/permissions", "show repository permission rules"),
    CommandSpec(
        "mode",
        "/mode ask|auto|readonly",
        "choose approval behavior",
        ("ask", "auto", "readonly"),
    ),
    CommandSpec("model", "/model", "select a model and reasoning effort"),
    CommandSpec("tools", "/tools", "list registered tools"),
    CommandSpec("skills", "/skills", "list discovered Skills"),
    CommandSpec("skill", "/skill <name>", "activate a Skill"),
    CommandSpec(
        "approve",
        "/approve once|always|deny",
        "resolve pending approval",
        ("once", "always", "deny"),
    ),
    CommandSpec(
        "steer",
        "/steer [now] <instruction>",
        "correct a running turn",
        ("now",),
    ),
    CommandSpec("new", "/new", "start a new session"),
    CommandSpec("resume", "/resume [id|name|prefix]", "select or resume a session"),
    CommandSpec("sessions", "/sessions", "list sessions"),
    CommandSpec("rename", "/rename <name>", "rename the current session"),
    CommandSpec("clear", "/clear", "clear the active session from the UI"),
    CommandSpec("stop", "/stop", "stop the active turn at a safe boundary"),
    CommandSpec("quit", "/quit", "exit InnoAgent"),
)

COMMAND_BY_NAME = {spec.name: spec for spec in COMMAND_SPECS}

OPTION_DESCRIPTIONS = {
    ("goal", "off"): "clear the active goal",
    ("goal", "clear"): "clear the active goal",
    ("mode", "ask"): "confirm write-capable operations",
    ("mode", "auto"): "run allowed operations without asking",
    ("mode", "readonly"): "block write-capable operations",
    ("approve", "once"): "allow only this operation",
    ("approve", "always"): "allow matching operations in this workspace",
    ("approve", "deny"): "deny the pending operation",
    ("steer", "now"): "apply before the next tool starts",
}


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
    """Return stable metadata for a slash command."""
    spec = COMMAND_BY_NAME.get(name)
    return {
        "known": spec is not None,
        "usage": spec.usage if spec else "",
        "description": spec.description if spec else "",
        "options": list(spec.options) if spec else [],
    }


def static_command_choices(name: str) -> list[CommandChoice]:
    """Return command arguments that do not depend on runtime state."""
    spec = COMMAND_BY_NAME.get(name)
    return [
        CommandChoice(
            value=value,
            description=OPTION_DESCRIPTIONS.get((name, value), ""),
        )
        for value in (spec.options if spec else ())
    ]


def command_help_text() -> str:
    """Build CLI help from the same command registry used by completion."""
    width = max(len(spec.usage) for spec in COMMAND_SPECS)
    lines = ["InnoAgent commands", ""]
    lines.extend(f"  {spec.usage:<{width}}  {spec.description}" for spec in COMMAND_SPECS)
    return "\n".join(lines)
