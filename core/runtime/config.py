"""Runtime configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.guardrails.policy import RunMode


@dataclass
class RuntimeConfig:
    """用户可配置的运行时参数"""
    workspace_root: str = "."  # 工作目录
    mode: RunMode = RunMode.ASK  # 权限询问模式
    max_iterations: int = 20  # 单次最大模型思考轮数
    max_reflections: int = 3  # 最大反思次数
    max_context_tokens: int = 128000  # 上下文最大token数
    compact_reserve_tokens: int = 16000  # 压缩后最大保留token数
    compact_threshold: float = 0.8  # 压缩阈值, 上下文百分比
    compact_keep_recent_tokens: int = 12000  # 保留的最近的token数
    max_tool_output_chars: int = 30000  # 工具最大输出字符数
    max_parallel_tools: int = 4  # 最大同时并行工具数
    profile_root: str = ".innoagent/profiles"  # 用户个人画像存储位置
    session_root: str = ".innoagent/sessions"  # session信息存储位置
    memory_enabled: bool = True  # 是否开启记忆
    turn_threshold: int = 16  # 记忆更新阈值
    input_token_threshold: int = 1200  # 输入token上限
    extra: dict = field(default_factory=dict)  # 额外配置

    @property
    def allowed_roots(self) -> list[str]:
        """Return the workspace path allowed for file tools."""
        return [str(Path(self.workspace_root).expanduser().resolve())]

    @property
    def compact_threshold_tokens(self) -> int:
        """Return the configured automatic-compaction boundary in tokens."""
        return max(
            1,
            min(
                self.max_context_tokens - 1,
                int(self.max_context_tokens * self.compact_threshold),
            ),
        )


class RuntimeConfigStore:
    """Persist non-secret runtime settings beside the project model config."""

    def __init__(self, project_root: str | Path = ".") -> None:
        self.path = Path(project_root).expanduser().resolve() / ".innoagent" / "config.json"

    def load(self, config: RuntimeConfig | None = None) -> RuntimeConfig:
        """Overlay valid project runtime settings on an existing config."""
        resolved = config or RuntimeConfig()
        data = self._read()
        runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
        try:
            max_tokens = int(runtime.get("max_context_tokens", resolved.max_context_tokens))
            threshold = float(runtime.get("compact_threshold", resolved.compact_threshold))
            keep_tokens = int(
                runtime.get("compact_keep_recent_tokens", resolved.compact_keep_recent_tokens)
            )
            self._validate(max_tokens, threshold, keep_tokens)
        except (TypeError, ValueError):
            return resolved
        resolved.max_context_tokens = max_tokens
        resolved.compact_threshold = threshold
        resolved.compact_keep_recent_tokens = keep_tokens
        resolved.compact_reserve_tokens = max(1, max_tokens - resolved.compact_threshold_tokens)
        return resolved

    def save(self, config: RuntimeConfig) -> Path:
        """Merge runtime settings without overwriting model credentials."""
        self._validate(
            config.max_context_tokens,
            config.compact_threshold,
            config.compact_keep_recent_tokens,
        )
        data = self._read()
        data["runtime"] = {
            "max_context_tokens": config.max_context_tokens,
            "compact_threshold": config.compact_threshold,
            "compact_keep_recent_tokens": config.compact_keep_recent_tokens,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.path

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _validate(max_tokens: int, threshold: float, keep_tokens: int) -> None:
        if max_tokens < 1024:
            raise ValueError("max context tokens must be at least 1024")
        if not 0.01 <= threshold < 1.0:
            raise ValueError("compact threshold must be between 1% and 99%")
        threshold_tokens = int(max_tokens * threshold)
        if keep_tokens < 1 or keep_tokens >= threshold_tokens:
            raise ValueError("kept recent tokens must be below the compact threshold")
