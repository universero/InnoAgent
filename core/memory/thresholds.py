"""Memory update threshold configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryThresholds:
    """Thresholds that trigger asynchronous memory updates."""
    turn_threshold: int = 4
    input_token_threshold: int = 1200

    def should_update(self, turns_since_update: int, tokens_since_update: int) -> bool:
        """Return whether enough turns or tokens have accumulated."""
        return (
            turns_since_update >= self.turn_threshold
            or tokens_since_update >= self.input_token_threshold
        )
