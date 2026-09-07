"""Helpers for aggregating token usage reported by model providers."""

from __future__ import annotations

from typing import Any, Mapping


TOKEN_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)


def empty_usage() -> dict[str, int]:
    """Return the stable session usage shape exposed to the UI."""
    return {key: 0 for key in TOKEN_USAGE_KEYS} | {"requests": 0}


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """Keep only non-negative integer counters from a provider response."""
    if not usage:
        return {}
    normalized: dict[str, int] = {}
    for key in TOKEN_USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            normalized[key] = max(0, value)
    requests = usage.get("requests")
    if isinstance(requests, int) and not isinstance(requests, bool):
        normalized["requests"] = max(0, requests)
    return normalized


def accumulate_usage(
    total: Mapping[str, Any] | None,
    usage: Mapping[str, Any] | None,
) -> dict[str, int]:
    """Add one response usage record to a session-level aggregate."""
    current = empty_usage()
    for key, value in normalize_usage(total).items():
        current[key] = value

    incoming = normalize_usage(usage)
    if not incoming:
        return current
    for key in TOKEN_USAGE_KEYS:
        current[key] += incoming.get(key, 0)
    current["requests"] += incoming.get("requests", 1)
    return current
