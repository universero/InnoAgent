"""Token and message-count budgeting."""

from __future__ import annotations

from core.session.history import Message


def estimate_tokens(text: str) -> int:
    """Use a deliberately simple token estimate.

    This estimate is not an exact tokenizer, but it is stable and sufficient
    for context budgeting in the first implementation.
    """
    return max(1, len(text) // 4)


def trim_messages(messages: list[Message], max_tokens: int) -> list[Message]:
    """Keep the most recent messages within the token budget."""
    result: list[Message] = []
    used = 0
    for message in reversed(messages):
        cost = estimate_tokens(message.content)
        if result and used + cost > max_tokens:
            break
        result.append(message)
        used += cost
    result.reverse()
    return result


def summarize_if_needed(messages: list[Message], max_tokens: int) -> tuple[list[Message], bool]:
    """Return trimmed messages and a flag indicating truncation occurred."""
    trimmed = trim_messages(messages, max_tokens)
    return trimmed, len(trimmed) != len(messages)
