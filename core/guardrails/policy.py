"""Execution-mode policy shared by the CLI and file guardrails."""

from __future__ import annotations

from typing import Literal


RunMode = Literal["auto", "confirm", "readonly"]

VALID_MODES: set[RunMode] = {"auto", "confirm", "readonly"}


def normalize_mode(mode: str) -> RunMode:
    """Return a valid run mode or fall back to ``auto``."""
    if mode not in VALID_MODES:
        return "auto"
    return mode  # type: ignore[return-value]


def should_confirm_write(mode: RunMode, tool_is_write: bool) -> bool:
    """Return whether a write operation requires explicit user confirmation."""
    return mode == "confirm" and tool_is_write


def is_readonly(mode: RunMode) -> bool:
    """Return whether the current mode disallows writes."""
    return mode == "readonly"
