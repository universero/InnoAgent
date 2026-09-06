"""User profile model and persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class UserFact(BaseModel):
    """A simple key/value fact about the user."""
    key: str
    value: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class UserProfile(BaseModel):
    """Minimal user profile persisted between sessions."""
    user_id: str = "default"
    preferences: dict[str, str] = Field(default_factory=dict)
    facts: list[UserFact] = Field(default_factory=list)
    recent_topics: list[str] = Field(default_factory=list)
    working_style: str | None = None
    version: int = 1
    last_updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def default_for(cls, user_id: str) -> "UserProfile":
        """Create an empty profile for a user."""
        return cls(user_id=user_id)

    def recall_text(self, max_chars: int = 600) -> str:
        """Render a compact memory block for the final context."""
        parts: list[str] = []
        if self.preferences:
            parts.append("用户偏好：" + "; ".join(f"{k}={v}" for k, v in self.preferences.items()))
        if self.facts:
            facts = "; ".join(f"{fact.key}={fact.value}" for fact in self.facts[:8])
            parts.append("长期信息：" + facts)
        if self.recent_topics:
            parts.append("近期话题：" + "、".join(self.recent_topics[:6]))
        if self.working_style:
            parts.append("工作风格：" + self.working_style)
        text = "\n".join(parts)
        return text[:max_chars]

    def apply_update(self, update: dict[str, Any]) -> "UserProfile":
        """Apply a small model-generated update."""
        profile = self.model_copy(deep=True)
        preferences = update.get("preferences") or {}
        if isinstance(preferences, dict):
            profile.preferences.update({str(k): str(v) for k, v in preferences.items()})

        facts = update.get("facts") or []
        existing_keys = {fact.key for fact in profile.facts}
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            key = str(fact.get("key", "")).strip()
            if not key:
                continue
            value = str(fact.get("value", ""))
            confidence = float(fact.get("confidence", 0.5))
            if key in existing_keys:
                for old in profile.facts:
                    if old.key == key:
                        old.value = value
                        old.confidence = max(old.confidence, confidence)
            else:
                profile.facts.append(UserFact(key=key, value=value, confidence=confidence))
                existing_keys.add(key)

        recent_topics = update.get("recent_topics") or []
        if isinstance(recent_topics, list):
            for topic in recent_topics:
                if str(topic) not in profile.recent_topics:
                    profile.recent_topics.insert(0, str(topic))
            profile.recent_topics = profile.recent_topics[:10]

        if update.get("working_style"):
            profile.working_style = str(update["working_style"])

        profile.version += 1
        profile.last_updated_at = datetime.now(timezone.utc)
        return profile


class ProfileStore:
    """Persist profiles as JSON files."""

    def __init__(self, root: str | Path = ".innoagent/profiles") -> None:
        """Store the profile directory."""
        self.root = Path(root)

    def _path(self, user_id: str) -> Path:
        """Return the JSON path for a user profile."""
        return self.root / f"{user_id}.json"

    def load(self, user_id: str) -> UserProfile:
        """Load a profile or return a default profile."""
        path = self._path(user_id)
        if path.exists():
            return UserProfile.model_validate_json(path.read_text(encoding="utf-8"))
        return UserProfile.default_for(user_id)

    def save(self, profile: UserProfile) -> None:
        """Persist a profile to disk."""
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(profile.user_id).write_text(profile.model_dump_json(indent=2), encoding="utf-8")
