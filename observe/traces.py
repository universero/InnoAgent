"""Minimal in-memory trace store."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TraceStore:
    """Collect and optionally persist trace events."""

    def __init__(self, file_path: str | Path | None = None) -> None:
        """Initialize an in-memory event list."""
        self.events: list[dict[str, Any]] = []
        self.file_path = Path(file_path) if file_path else None

    def record(self, event: str, data: dict[str, Any]) -> None:
        """Append a trace event."""
        entry = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed": time.monotonic(),
        }
        self.events.append(entry)
        if self.file_path:
            self._append(entry)

    def _append(self, entry: dict[str, Any]) -> None:
        """Write one trace event to the JSONL file."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
        with self.file_path.open("a", encoding="utf-8") as handle:  # type: ignore[union-attr]
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def latest(self, event: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent events, optionally filtered by name."""
        events = [item for item in self.events if event is None or item["event"] == event]
        return events[-limit:]
