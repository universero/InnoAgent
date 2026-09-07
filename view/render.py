"""Text rendering helpers for the CLI."""

from __future__ import annotations

from typing import Any

from core.runtime.agent import InnoAgentRuntime
from core.session.store import SessionRecord
from observe.traces import TraceStore


def render_events(tracer: TraceStore, start_index: int) -> list[str]:
    """Render new trace events in compact process-display form."""
    lines: list[str] = []
    for event in tracer.events[start_index:]:
        name = event["event"]
        data = event.get("data", {})
        if name == "execute":
            action = data.get("action", "")
            tool_calls = ", ".join(data.get("tool_calls", []))
            if action == "finish":
                continue
            if tool_calls:
                lines.append(f"[执行] 准备调用：{tool_calls}")
            elif action == "planning":
                lines.append("[执行] 进入计划")
            elif action == "reflection":
                lines.append("[执行] 进入反思")
            else:
                lines.append("[执行] 继续处理")
        elif name == "planning":
            lines.append(f"[计划] 已生成/更新，revision={data.get('revision', '?')}")
        elif name == "reflection":
            if data.get("complete"):
                lines.append("[反思] 检查完成")
            else:
                lines.append("[反思] 需要继续执行")
        elif name == "tool_use":
            tool_name = data.get("tool_name") or data.get("tool") or "?"
            function = data.get("function") or tool_name
            status = data.get("status", "?")
            summary = data.get("summary") or status
            lines.append(f"[工具] 名称：{tool_name}")
            lines.append(f"       函数：{function}")
            lines.append("       结果：")
            if summary:
                for output_line in summary.splitlines():
                    lines.append(f"         {output_line}")
            else:
                lines.append(f"         {status}")
            warnings = data.get("warnings") or []
            if warnings:
                lines.append(f"       警告：{'；'.join(warnings)}")
    return lines


def render_state(state: dict[str, Any]) -> str:
    """Render the user-facing part of a finished state."""
    response = state.get("response", "")
    streamed_response = state.get("streamed_response", "")
    tool_results = state.get("tool_results") or []
    lines: list[str] = []
    if response and response.strip() != streamed_response.strip():
        lines.append(response)
    elif not response and not streamed_response and tool_results:
        last_result = next(
            (
                item
                for item in reversed(tool_results)
                if item.get("status") == "success" and item.get("output")
            ),
            None,
        )
        if last_result:
            lines.append(last_result["output"])
    plan = state.get("plan")
    if plan:
        steps = plan.get("steps", [])
        lines.append(
            f"计划: {plan.get('status', 'active')} rev{plan.get('revision', 1)}，共 {len(steps)} 步"
        )
    tasks = state.get("tasks") or []
    if tasks:
        for task in tasks:
            marker = {"done": "✓", "pending": "·", "in_progress": "▶", "blocked": "!"}.get(
                task.get("status", "pending"), "·"
            )
            lines.append(f"  {marker} {task.get('title', '')}")
    return "\n".join(lines)


def render_tools(runtime: InnoAgentRuntime, state: dict[str, Any] | None = None) -> str:
    """Render the current tool inventory for the CLI."""
    from core.tool.base import ToolContext

    context = ToolContext(
        mode=runtime.config.normalized_mode,
        allowed_roots=runtime.config.allowed_roots,
        state=state or {},
    )
    lines = ["可用工具："]
    for schema in runtime.registry.tool_schemas(context):
        write = " [write]" if schema.get("is_write") else ""
        lines.append(f"  {schema['name']}{write}: {schema['description']}")
    return "\n".join(lines)


def render_sessions(records: list[SessionRecord]) -> str:
    """Render the list of resumable sessions."""
    if not records:
        return "没有可恢复的 session。"
    lines = ["可恢复 session："]
    for record in records:
        goal = record.goal or "无 goal"
        label = record.name or record.session_id
        lines.append(
            f"  {label}  id={record.session_id}  mode={record.mode}  goal={goal}"
        )
    return "\n".join(lines)


def render_event(event: dict[str, Any]) -> str:
    """Render one structured runtime event for the CLI."""
    event_type = event.get("type", "")
    if event_type == "item.delta":
        return ""
    if event_type in {"turn.started", "response.completed", "state.checkpoint"}:
        return ""
    if event_type == "item.started" and event.get("item_type") == "tool_call":
        return f"[tool] {event.get('tool_name') or '?'}"
    if event_type == "item.completed":
        item_type = event.get("item_type")
        payload = event.get("payload") or {}
        stage = event.get("stage", "main")
        if item_type == "message":
            if payload.get("streamed"):
                return ""
            content = str(payload.get("content") or event.get("content") or "")
            return f"[{stage}] {content}" if stage != "main" else content
        if item_type == "tool_call":
            tool_name = event.get("tool_name") or payload.get("name") or "?"
            arguments = event.get("arguments") or payload.get("arguments") or {}
            arguments_text = ", ".join(f"{key}={value}" for key, value in arguments.items())
            return f"[tool call] {tool_name}({arguments_text})"
        if item_type == "tool_result":
            result = payload.get("result") or event.get("result") or {}
            tool_name = event.get("tool_name") or result.get("tool_name") or "?"
            status = result.get("status", "?")
            output = str(result.get("output") or "")
            lines = [f"[tool {status}] {tool_name}"]
            if output:
                lines.extend(f"  {line}" for line in output.splitlines())
            for warning in result.get("warnings") or []:
                lines.append(f"  warning: {warning}")
            return "\n".join(lines)
        if item_type == "plan":
            plan = payload.get("plan") or {}
            tasks = payload.get("tasks") or []
            return f"[plan] revision {plan.get('revision', '?')} · {len(tasks)} tasks"
        if item_type == "reflection":
            status = "complete" if payload.get("complete") else "continue"
            detail = payload.get("summary") or payload.get("feedback") or ""
            return f"[reflect {status}] {detail}".rstrip()
        if item_type == "user_question":
            question = payload.get("question") or payload.get("content", "")
            options = event.get("options") or payload.get("options") or []
            choices = f"\n  options: {' / '.join(options)}" if options else ""
            return f"[question] {question}{choices}"
    if event_type == "approval.requested":
        payload = event.get("payload") or {}
        tool_name = event.get("tool_name") or payload.get("tool_name") or "?"
        return (
            f"[permission] {tool_name} requires approval\n"
            "  allow once / always allow here / deny"
        )
    if event_type == "approval.resolved":
        return f"[permission] {event.get('payload', {}).get('decision', 'resolved')}"
    if event_type == "context.compaction.started":
        return "[compact] summarizing older context..."
    if event_type == "context.compaction.completed":
        payload = event.get("payload") or {}
        ratio = float(payload.get("compression_ratio", 1.0)) * 100
        return (
            f"[compact] {payload.get('tokens_before', 0)} -> "
            f"{payload.get('tokens_after', 0)} tokens ({ratio:.1f}% retained)"
        )
    if event_type == "steering.queued":
        delivery = event.get("payload", {}).get("delivery", "after_tool")
        return f"[steer queued] delivery={delivery}"
    if event_type == "steering.applied":
        payload = event.get("payload", {})
        return f"[steer applied] {payload.get('content', '')}"
    if event_type == "steering.stop_requested":
        return "[stop] will stop at the next safe boundary"
    if event_type == "steering.stopped":
        return "[stop] turn stopped"
    if event_type == "turn.failed":
        return f"[error] {event.get('payload', {}).get('finish_reason', 'turn failed')}"
    if event_type == "tool":
        tool_name = event.get("tool_name") or "?"
        arguments = event.get("arguments") or {}
        arguments_text = ", ".join(f"{key}={value}" for key, value in arguments.items())
        if event.get("phase") == "call":
            return (
                f"[工具] 名称：{tool_name}\n"
                f"       参数：{arguments_text or '无'}"
            )
        summary = event.get("summary") or event.get("status") or ""
        lines = [
            f"[工具] 名称：{tool_name}",
            f"       参数：{arguments_text or '无'}",
            "       结果：",
        ]
        if summary:
            lines.extend(f"         {line}" for line in summary.splitlines())
        else:
            lines.append(f"         {event.get('status', '?')}")
        warnings = event.get("warnings") or []
        if warnings:
            lines.append(f"       警告：{'；'.join(warnings)}")
        return "\n".join(lines)
    if event_type == "permission_confirm":
        return f"[权限确认] {event.get('tool_name', '?')}：{event.get('content', '需要用户批准')}"
    if event_type == "user_confirm":
        options = event.get("options") or []
        if options:
            return f"[询问] {event.get('content', '')} 选项：{' / '.join(options)}"
        return f"[询问] {event.get('content', '')}"
    if event_type in {"plan", "reflection", "model_decision"}:
        return f"[计划] 已生成/更新，revision={event.get('revision', '?')}"
    if event_type in {"needs_confirmation", "final_result", "tool_result", "model_text"}:
        return ""
    return ""
