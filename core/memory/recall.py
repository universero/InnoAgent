"""Memory recall used by context assembly."""

from __future__ import annotations

from core.memory.profile import ProfileStore, UserProfile


class MemoryRecall:
    """Read user profiles for context assembly."""

    def __init__(self, store: ProfileStore) -> None:
        """Store the profile backend."""
        self.store = store

    def recall(self, user_id: str) -> UserProfile:
        """Return the profile for a user."""
        return self.store.load(user_id)

    def recall_text(self, user_id: str, max_chars: int = 600) -> str:
        """Return a compact memory block for the model prompt."""
        return self.recall(user_id).recall_text(max_chars=max_chars)
