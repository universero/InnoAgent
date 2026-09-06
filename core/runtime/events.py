"""Structured event helpers shared by runtime, session log and CLI."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def make_event(event_type: str, **data: Any) -> dict[str, Any]:
    """Create a JSON-serializable event object."""
    return {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data,
    }
