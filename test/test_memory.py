"""Memory profile and update tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory.profile import ProfileStore, UserProfile
from core.memory.thresholds import MemoryThresholds
from core.memory.update import MemoryUpdater, heuristic_update


class MemoryTest(unittest.TestCase):
    """Tests for memory thresholds and profile updates."""

    def test_threshold_triggers_on_turns(self) -> None:
        """Verify turn-based update thresholds."""
        thresholds = MemoryThresholds(turn_threshold=2, input_token_threshold=10000)
        self.assertFalse(thresholds.should_update(1, 10))
        self.assertTrue(thresholds.should_update(2, 10))

    def test_profile_update(self) -> None:
        """Verify profile versions and facts after an update."""
        profile = UserProfile.default_for("u1")
        updated = profile.apply_update(heuristic_update(profile, "创建 app.py", "done"))
        self.assertGreater(updated.version, profile.version)
        self.assertTrue(any(fact.key == "last_request" for fact in updated.facts))

    def test_profile_store_roundtrip(self) -> None:
        """Verify profile JSON persistence."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(tmp)
            profile = store.load("u1")
            profile.preferences["lang"] = "zh"
            store.save(profile)
            self.assertEqual(store.load("u1").preferences["lang"], "zh")

    def test_updater_sync_update(self) -> None:
        """Verify synchronous profile updates."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(tmp)
            updater = MemoryUpdater(store, enabled=True)
            updated = updater.update_now("u1", "hello", "world")
            self.assertGreater(updated.version, 1)


if __name__ == "__main__":
    unittest.main()
