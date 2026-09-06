"""Asynchronous profile update.

The main graph is synchronous and should never block on memory writes.  This
module therefore provides a tiny background update mechanism using a worker
thread.  It can later be replaced by a queue-backed worker without changing the
session/runtime contract.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from core.memory.profile import ProfileStore, UserProfile
from core.memory.thresholds import MemoryThresholds


def heuristic_update(profile: UserProfile, user_input: str, response: str) -> dict[str, Any]:
    """Build a conservative memory update without an LLM.

    The real system may use a model for this step.  Keeping the function pure
    makes the fallback deterministic and testable.
    """
    topic = user_input.strip()[:40]
    facts = [
        {
            "key": "last_request",
            "value": user_input.strip()[:200],
            "confidence": 0.6,
        }
    ]
    recent_topics = [topic] if topic else []
    if len(response) < 300 and response.strip():
        facts.append({"key": "last_summary", "value": response.strip(), "confidence": 0.5})
    return {"facts": facts, "recent_topics": recent_topics}


class MemoryUpdater:
    """Update profiles in a daemon thread and persist them."""

    def __init__(
        self,
        store: ProfileStore,
        thresholds: MemoryThresholds | None = None,
        update_fn: Callable[[UserProfile, str, str], dict[str, Any]] | None = None,
        enabled: bool = True,
    ) -> None:
        """Configure the update backend and counters."""
        self.store = store
        self.thresholds = thresholds or MemoryThresholds()
        self.update_fn = update_fn or heuristic_update
        self.enabled = enabled
        self.turns_since_update = 0
        self.tokens_since_update = 0

    def register_turn(self, user_input: str, response: str = "", user_id: str = "default") -> bool:
        """Record one turn and trigger an update when thresholds are crossed."""
        self.turns_since_update += 1
        self.tokens_since_update += max(1, len(user_input) // 4)
        if not self.thresholds.should_update(self.turns_since_update, self.tokens_since_update):
            return False
        self._schedule(user_id, user_input, response)
        return True

    def _schedule(self, user_id: str, user_input: str, response: str) -> None:
        """Start a background thread when an update is due."""
        if not self.enabled:
            return
        thread = threading.Thread(
            target=self._update,
            args=(user_id, user_input, response),
            daemon=True,
            name=f"memory-update-{user_id}",
        )
        thread.start()

    def _update(self, user_id: str, user_input: str, response: str) -> None:
        """Apply and persist one asynchronous profile update."""
        profile = self.store.load(user_id)
        update = self.update_fn(profile, user_input, response)
        profile = profile.apply_update(update)
        self.store.save(profile)
        self.turns_since_update = 0
        self.tokens_since_update = 0

    def update_now(self, user_id: str, user_input: str, response: str) -> UserProfile:
        """Synchronous update used by tests and explicit CLI actions."""
        profile = self.store.load(user_id)
        update = self.update_fn(profile, user_input, response)
        profile = profile.apply_update(update)
        self.store.save(profile)
        self.turns_since_update = 0
        self.tokens_since_update = 0
        return profile
