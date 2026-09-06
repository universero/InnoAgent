"""Execution-mode policy shared by the CLI and file guardrails."""

from __future__ import annotations

from typing import Literal


RunMode = Literal["auto", "ask", "confirm", "readonly"]

VALID_MODES: set[RunMode] = {"auto", "ask", "confirm", "readonly"}


def normalize_mode(mode: str) -> RunMode:
    """Return a valid run mode or fall back to ``auto``."""
    if mode == "confirm":
        return "ask"
    if mode not in VALID_MODES:
        return "ask"
    return mode  # type: ignore[return-value]


def should_confirm_write(mode: RunMode, tool_is_write: bool) -> bool:
    """Return whether a write operation requires explicit user confirmation."""
    return mode in {"ask", "confirm"} and tool_is_write


def is_readonly(mode: RunMode) -> bool:
    """Return whether the current mode disallows writes."""
    return mode == "readonly"
