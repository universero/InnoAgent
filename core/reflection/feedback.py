"""Feedback generation helpers."""

from __future__ import annotations

from core.reflection.schemas import ReflectionResult


def feedback_to_message(result: ReflectionResult) -> str:
    """Convert reflection output into a model-facing message."""
    if result.complete:
        return "Reflection: goal is complete. Return the final answer to the user."

    lines = ["Reflection feedback:", result.feedback]
    if result.missing_conditions:
        lines.append("Missing conditions:")
        lines.extend(f"- {item}" for item in result.missing_conditions)
    if result.evidence:
        lines.append("Current evidence:")
        lines.extend(f"- {item}" for item in result.evidence)
    return "\n".join(lines)
