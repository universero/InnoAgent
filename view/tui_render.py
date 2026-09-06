"""Pure presentation helpers for the terminal UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit.formatted_text import StyleAndTextTuples


@dataclass(frozen=True)
class EventPresentation:
    """A semantic update consumed by TerminalIO."""

    activity: str | None = None
    stream: tuple[str, str] | None = None
    block: tuple[str, str, str] | None = None


def present_event(event: dict[str, Any], rendered: str = "") -> EventPresentation:
    """Convert a runtime event into one concise TUI operation."""
    event_type = str(event.get("type") or "")
    item_type = str(event.get("item_type") or "")
    payload = event.get("payload") or {}
    stage = str(event.get("stage") or "main")

    if event_type == "item.delta" and item_type in {"message", "reasoning"}:
        channel = stage if stage in {"plan", "reflect"} else item_type
        activity = {"plan": "Planning", "reflect": "Reflecting"}.get(stage)
        content = str(event.get("delta") or event.get("content") or "")
        return EventPresentation(activity=activity, stream=(channel, content))
    if event_type == "turn.started":
        return EventPresentation(activity="Thinking")
    if event_type == "turn.completed":
        return EventPresentation(activity="Ready")
    if event_type == "item.started" and item_type == "tool_call":
        return EventPresentation(activity=f"Tool · {event.get('tool_name') or '?'}")
    if event_type == "item.completed" and item_type == "tool_call":
        name = str(event.get("tool_name") or payload.get("name") or "?")
        arguments = event.get("arguments") or payload.get("arguments") or {}
        return EventPresentation(block=("tool", f"Tool · {name}", format_arguments(arguments)))
    if event_type == "item.completed" and item_type == "tool_result":
        result = payload.get("result") or event.get("result") or {}
        name = str(event.get("tool_name") or result.get("tool_name") or "?")
        status = str(result.get("status") or "unknown")
        tone = "success" if status == "success" else "error"
        body = compact_body(str(result.get("output") or ""))
        warnings = result.get("warnings") or []
        if warnings:
            body = "\n".join(filter(None, [body, *(f"warning: {item}" for item in warnings)]))
        return EventPresentation(activity="Thinking", block=(tone, f"{name} · {status}", body))
    if event_type == "item.completed" and item_type == "message":
        content = str(payload.get("content") or event.get("content") or "")
        block = None if payload.get("streamed") or not content else ("agent", "Agent", content)
        return EventPresentation(block=block)
    if event_type == "item.completed" and item_type == "plan":
        plan = payload.get("plan") or {}
        tasks = payload.get("tasks") or []
        return EventPresentation(
            activity="Planning",
            block=(
                "plan",
                f"Plan · revision {plan.get('revision', '?')}",
                f"{len(tasks)} tasks generated",
            ),
        )
    if event_type == "item.completed" and item_type == "reflection":
        complete = bool(payload.get("complete"))
        detail = str(payload.get("summary") or payload.get("feedback") or "")
        return EventPresentation(
            activity="Reflecting",
            block=(
                "success" if complete else "plan",
                "Reflection · complete" if complete else "Reflection · continue",
                detail,
            ),
        )
    if event_type == "approval.requested":
        name = str(event.get("tool_name") or payload.get("tool_name") or "tool")
        return EventPresentation(
            activity="Approval required",
            block=("warning", f"Approval · {name}", "Choose 1, 2, or 3 below."),
        )
    if event_type == "approval.resolved":
        return EventPresentation(
            block=("success", "Approval", str(payload.get("decision") or "resolved"))
        )
    if event_type == "context.compaction.started":
        return EventPresentation(
            activity="Compacting context",
            block=("muted", "Context", rendered),
        )
    if event_type == "context.compaction.completed":
        return EventPresentation(block=("muted", "Context compacted", rendered))
    if event_type == "steering.queued":
        delivery = str(payload.get("delivery") or "after_tool")
        return EventPresentation(block=("warning", "Steering queued", f"delivery: {delivery}"))
    if event_type == "steering.applied":
        return EventPresentation(
            block=("success", "Steering applied", str(payload.get("content") or ""))
        )
    if event_type == "steering.stop_requested":
        return EventPresentation(
            block=("warning", "Stop requested", "Waiting for a safe boundary.")
        )
    if event_type == "steering.stopped":
        return EventPresentation(
            block=("warning", "Stopped", "The active turn has stopped safely.")
        )
    if event_type == "turn.failed":
        return EventPresentation(activity="Failed", block=("error", "Error", rendered))
    if rendered:
        return EventPresentation(block=("muted", "Event", rendered))
    return EventPresentation()


def sidebar_fragments(snapshot: dict[str, Any], model: str, mode: str) -> StyleAndTextTuples:
    """Build the context rail from the latest stable runtime snapshot."""
    state = snapshot.get("state") or {}
    context = state.get("context_usage") or {}
    usage = state.get("usage") or {}
    percent = max(0.0, min(100.0, float(context.get("percent_used", 0))))
    used = int(context.get("used_tokens", 0))
    maximum = int(context.get("max_tokens", snapshot.get("max_context_tokens", 0)))
    tasks = state.get("tasks") or []
    goal = str(state.get("goal") or snapshot.get("goal") or "Not set")
    session = str(snapshot.get("session_id") or "New session")
    filled = round(percent / 100 * 18)
    bar = "█" * filled + "░" * (18 - filled)
    rows: StyleAndTextTuples = []

    def section(title: str) -> None:
        if rows:
            rows.append(("", "\n"))
        rows.append(("class:sidebar.heading", f" {title}\n"))

    def value(label: str, text: str, style: str = "class:sidebar.value") -> None:
        rows.extend(
            [
                ("class:sidebar.label", f" {label:<9}"),
                (style, f"{text}\n"),
            ]
        )

    section("SESSION")
    value("id", clip(session, 22))
    value("model", clip(str(snapshot.get("model") or model), 22))
    value("mode", str(snapshot.get("mode") or mode))
    value("tokens", str(usage.get("total_tokens", 0)))
    section("CONTEXT")
    rows.append(("class:sidebar.active", f" {bar} {percent:>3.0f}%\n"))
    rows.append(("class:sidebar.label", f" {used:,} / {maximum:,} tokens\n"))
    section("GOAL")
    rows.append(("class:sidebar.value", f" {clip(goal, 31)}\n"))
    section("TASKS")
    if not tasks:
        rows.append(("class:sidebar.label", " No active tasks\n"))
    for task in tasks[:8]:
        status = str(task.get("status") or "pending")
        marker, style = {
            "done": ("✓", "class:sidebar.good"),
            "in_progress": ("▶", "class:sidebar.active"),
            "blocked": ("!", "class:sidebar.blocked"),
        }.get(status, ("·", "class:sidebar.label"))
        rows.append((style, f" {marker} {clip(str(task.get('title') or ''), 28)}\n"))
    if len(tasks) > 8:
        rows.append(("class:sidebar.label", f"   +{len(tasks) - 8} more\n"))
    return rows


def header_fragments(cwd: str, activity: str, busy: bool) -> StyleAndTextTuples:
    status_style = "class:header.busy" if busy else "class:header.ready"
    return [
        ("class:header.brand", " INNOAGENT "),
        ("class:header.path", f"  {Path(cwd).name or cwd}"),
        ("class:header", "  ·  "),
        (status_style, activity),
    ]


def approval_fragments(approval: dict[str, str] | None) -> StyleAndTextTuples:
    value = approval or {}
    return [
        ("class:approval.title", f" APPROVAL · {value.get('tool', 'tool')} "),
        ("class:approval", f" {value.get('detail') or 'This operation needs permission.'}\n "),
        ("class:approval.key", " 1 Allow once "),
        ("class:approval", "  "),
        ("class:approval.key", " 2 Always here "),
        ("class:approval", "  "),
        ("class:approval.key", " 3 Deny "),
    ]


def status_fragments() -> StyleAndTextTuples:
    rows: StyleAndTextTuples = []
    for key, action in (
        ("Enter", "send"),
        ("Tab", "complete"),
        ("Ctrl-C", "stop"),
        ("Ctrl-L", "clear"),
        ("F1", "help"),
        ("Ctrl-Q", "quit"),
    ):
        rows.extend(
            [
                ("class:status.key", f" {key}"),
                ("class:status", f" {action} "),
            ]
        )
    return rows


def format_arguments(arguments: dict[str, Any]) -> str:
    parts = []
    for key, value in arguments.items():
        compact = " ".join(str(value).split())
        parts.append(f"{key}={clip(compact, 120)}")
    return ", ".join(parts)


def compact_body(value: str, max_lines: int = 14, max_chars: int = 1800) -> str:
    lines = value.splitlines()
    clipped = "\n".join(lines[:max_lines])
    if len(clipped) > max_chars:
        clipped = clipped[:max_chars]
    if len(lines) > max_lines or len(value) > len(clipped):
        omitted = max(0, len(value) - len(clipped))
        clipped = f"{clipped}\n… {omitted} chars omitted"
    return clipped


def clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"
