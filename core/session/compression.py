"""Token budgeting and structured conversation compaction."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from core.prompts import COMPACTION_PROMPT
from core.session.history import Message


def estimate_tokens(text: str) -> int:
    """Use a deliberately simple token estimate.

    This estimate is not an exact tokenizer, but it is stable and sufficient
    for context budgeting in the first implementation.
    """
    return max(1, len(text) // 4)


def estimate_message_tokens(message: Message) -> int:
    """Estimate content plus structured tool-call linkage."""
    return estimate_tokens(
        message.content
        + json.dumps(message.tool_calls, ensure_ascii=False)
        + str(message.tool_call_id or "")
        + str(message.name or "")
    )


def trim_messages(messages: list[Message], max_tokens: int) -> list[Message]:
    """Keep the most recent messages within the token budget."""
    result: list[Message] = []
    used = 0
    for message in reversed(messages):
        cost = estimate_message_tokens(message)
        if result and used + cost > max_tokens:
            break
        result.append(message)
        used += cost
    result.reverse()
    start = len(messages) - len(result)
    while start > 0 and result and result[0].role != "user":
        start -= 1
        result.insert(0, messages[start])
    return result


def summarize_if_needed(messages: list[Message], max_tokens: int) -> tuple[list[Message], bool]:
    """Return trimmed messages and a flag indicating truncation occurred."""
    trimmed = trim_messages(messages, max_tokens)
    return trimmed, len(trimmed) != len(messages)


@dataclass(frozen=True)
class CompactionResult:
    """Messages and metrics produced by one compaction pass."""

    messages: list[Message]
    summary: str
    tokens_before: int
    tokens_after: int

    @property
    def compression_ratio(self) -> float:
        if self.tokens_before <= 0:
            return 1.0
        return round(self.tokens_after / self.tokens_before, 4)


class ContextCompactor:
    """Replace older turns with a structured summary and keep recent context."""

    def __init__(self, keep_recent_tokens: int = 12000) -> None:
        self.keep_recent_tokens = keep_recent_tokens

    def compact(
        self,
        messages: list[Message],
        summarizer: Callable[[str], str] | None = None,
        *,
        focus: str | None = None,
    ) -> CompactionResult:
        tokens_before = sum(estimate_message_tokens(message) for message in messages)
        recent = trim_messages(messages, self.keep_recent_tokens)
        split = max(0, len(messages) - len(recent))
        older = messages[:split]
        if not older:
            return CompactionResult(
                messages=list(messages),
                summary="",
                tokens_before=tokens_before,
                tokens_after=tokens_before,
            )

        serialized = serialize_messages(older)
        request = COMPACTION_PROMPT
        if focus:
            request += f"\n\nAdditional focus: {focus.strip()}"
        request += "\n\nConversation to summarize:\n" + serialized
        summary = ""
        if summarizer is not None:
            try:
                summary = summarizer(request).strip()
            except Exception:
                summary = ""
        if not summary:
            summary = fallback_summary(older)
        compacted = [Message(role="summary", content=summary), *recent]
        tokens_after = sum(estimate_message_tokens(message) for message in compacted)
        return CompactionResult(
            messages=compacted,
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )


def serialize_messages(messages: list[Message], max_tool_chars: int = 2000) -> str:
    """Serialize history as data so the summarizer does not continue it."""
    parts: list[str] = []
    for message in messages:
        content = message.content
        if message.role == "tool" and len(content) > max_tool_chars:
            omitted = len(content) - max_tool_chars
            content = content[:max_tool_chars] + f"\n[... {omitted} characters truncated]"
        parts.append(f"[{message.role}]: {content}")
    return "\n\n".join(parts)


def fallback_summary(messages: list[Message]) -> str:
    """Deterministic safety net when the model summary is unavailable."""
    lines = ["Goal and user intent"]
    user_messages = [message.content for message in messages if message.role == "user"]
    lines.append(f"- {user_messages[-1] if user_messages else 'Not explicitly recorded.'}")
    lines.extend(
        [
            "Constraints and permissions",
            "- Preserve the active runtime permission policy.",
            "Decisions and rationale",
            "- Earlier details were compacted without a model-generated summary.",
            "Plan and task progress",
            "- Re-read the injected goal and plan state.",
            "Files read or modified",
            "- Consult retained tool results and repository state.",
            "Tool results and verification",
            "- Recent results are retained after this summary.",
            "Open issues and exact next steps",
            "- Continue from the most recent retained turn and verify assumptions.",
        ]
    )
    return "\n".join(lines)
