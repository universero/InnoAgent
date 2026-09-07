"""Pure presentation helpers for the terminal UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
        if stage in {"plan", "reflect"}:
            # Planning/Reflection 的模型输出是内部 JSON 协议，只展示阶段状态，
            # 最终内容由对应的语义完成事件渲染。
            return EventPresentation(
                activity="Planning" if stage == "plan" else "Reflecting"
            )
        content = str(event.get("delta") or event.get("content") or "")
        return EventPresentation(stream=(item_type, content))
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
    if (
        event_type == "item.completed"
        and item_type in {"message", "reasoning"}
        and stage in {"plan", "reflect"}
    ):
        return EventPresentation(
            activity="Planning" if stage == "plan" else "Reflecting"
        )
    if event_type == "item.completed" and item_type == "message":
        content = str(payload.get("content") or event.get("content") or "")
        block = None if payload.get("streamed") or not content else ("agent", "Agent", content)
        return EventPresentation(block=block)
    if event_type == "item.completed" and item_type == "reasoning":
        content = str(payload.get("content") or event.get("content") or "")
        block = (
            None
            if payload.get("streamed") or not content
            else ("thinking", "Thinking", content)
        )
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
        tone, title, body = format_reflection(payload)
        return EventPresentation(
            activity="Reflecting",
            block=(tone, title, body),
        )
    if event_type == "item.completed" and item_type == "user_question":
        question = str(
            payload.get("question")
            or payload.get("content")
            or event.get("content")
            or ""
        )
        return EventPresentation(
            activity="Waiting for answer",
            block=("warning", "Question", question),
        )
    if event_type == "approval.requested":
        # 具体参数和选项由 TerminalIO.set_approval() 一次性展示，
        # 避免重复卡片。
        return EventPresentation(activity="Approval required")
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


def format_arguments(arguments: dict[str, Any]) -> str:
    parts = []
    for key, value in arguments.items():
        compact = " ".join(str(value).split())
        parts.append(f"{key}={clip(compact, 120)}")
    return ", ".join(parts)


def format_reflection(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Render a structured reflection result without exposing its JSON protocol."""
    complete = bool(payload.get("complete"))
    blocked = bool(payload.get("blocked"))
    needs_user = bool(payload.get("needs_user"))
    if complete:
        tone, title = "success", "Reflection · Goal complete"
    elif blocked:
        tone, title = "error", "Reflection · Blocked"
    elif needs_user:
        tone, title = "warning", "Reflection · Needs input"
    else:
        tone, title = "plan", "Reflection · Continue"

    lines: list[str] = []
    confidence = payload.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        percent = round(max(0.0, min(1.0, float(confidence))) * 100)
        lines.append(f"Confidence {percent}%")

    summary = str(payload.get("summary") or "").strip()
    feedback = str(payload.get("feedback") or "").strip()
    question = str(payload.get("question") or "").strip()
    if summary:
        lines.extend(["", summary] if lines else [summary])
    if feedback and feedback != summary:
        lines.extend(["", f"Next: {feedback}"])
    if question:
        lines.extend(["", f"Question: {question}"])

    _append_list(lines, "Missing", payload.get("missing_conditions"))
    _append_list(lines, "Evidence", payload.get("evidence"))
    return tone, title, compact_body("\n".join(lines), max_lines=16, max_chars=2200)


def _append_list(lines: list[str], title: str, values: Any) -> None:
    if not isinstance(values, (list, tuple)):
        return
    items = [str(item).strip() for item in (values or []) if str(item).strip()]
    if not items:
        return
    lines.extend(["", title])
    lines.extend(f"• {clip(item, 220)}" for item in items[:6])
    if len(items) > 6:
        lines.append(f"• … {len(items) - 6} more")


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
