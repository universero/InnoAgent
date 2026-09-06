"""Runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.guardrails.policy import RunMode, normalize_mode


@dataclass
class RuntimeConfig:
    """User-configurable runtime settings."""
    workspace_root: str = "."
    mode: str = "ask"
    max_iterations: int = 20
    max_reflections: int = 3
    max_context_tokens: int = 128000
    compact_reserve_tokens: int = 16000
    compact_keep_recent_tokens: int = 12000
    max_tool_output_chars: int = 30000
    max_parallel_tools: int = 4
    profile_root: str = ".innoagent/profiles"
    session_root: str = ".innoagent/sessions"
    memory_enabled: bool = True
    turn_threshold: int = 4
    input_token_threshold: int = 1200
    extra: dict = field(default_factory=dict)

    @property
    def normalized_mode(self) -> RunMode:
        """Return a validated run mode."""
        return normalize_mode(self.mode)

    @property
    def allowed_roots(self) -> list[str]:
        """Return the workspace path allowed for file tools."""
        return [str(Path(self.workspace_root).expanduser().resolve())]
