"""Unified runtime and model configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from core.guardrails.policy import RunMode


@dataclass
class RuntimeConfig:
    """Resolved runtime and model settings."""

    workspace_root: str = "."  # 工作目录
    mode: RunMode = RunMode.ASK  # 权限询问模式
    max_iterations: int = 20  # 单次最大模型思考轮数
    max_reflections: int = 3  # 最大反思次数
    max_context_tokens: int = 128000  # 上下文最大token数
    compact_threshold: float = 0.8  # 压缩阈值, 上下文百分比
    compact_keep_recent_tokens: int = 12000  # 保留的最近的token数
    max_tool_output_chars: int = 30000  # 工具最大输出字符数
    max_parallel_tools: int = 4  # 最大同时并行工具数
    profile_root: str = ".innoagent/profiles"  # 用户个人画像存储位置
    session_root: str = ".innoagent/sessions"  # session信息存储位置
    memory_enabled: bool = True  # 是否开启记忆
    turn_threshold: int = 16  # 记忆更新阈值
    input_token_threshold: int = 1200  # 输入token上限

    api_key: str = ""  # API Key
    base_url: str = "https://api.deepseek.com"  # 模型服务地址
    model: str = "deepseek-v4-flash"  # 模型名称
    reasoning_effort: str = "none"  # 思考强度

    @property
    def allowed_roots(self) -> list[str]:
        return [str(Path(self.workspace_root).expanduser().resolve())]

    @property
    def compact_threshold_tokens(self) -> int:
        return max(1, min(self.max_context_tokens - 1, int(self.max_context_tokens * self.compact_threshold)))


class ConfigStore:
    """Load and persist all persistent settings in one YAML file."""

    def __init__(
        self,
        project_root: str | Path = ".",
        global_root: str | Path | None = None,
        input_fn: Callable[[str], str] | None = None,
        output_fn: Callable[[str], None] | None = None,
    ) -> None:
        project = Path(project_root).expanduser().resolve()
        self.project_path = project / ".innoagent" / "config.yaml"
        self.global_path = (Path(global_root) if global_root else Path.home() / ".innoagent") / "config.yaml"
        self.input_fn = input_fn or input
        self.output_fn = output_fn or print

    def load(self, config: RuntimeConfig | None = None) -> RuntimeConfig:
        resolved = config or RuntimeConfig()
        data = self._read_yaml(self.project_path) or self._read_yaml(self.global_path)
        self._apply(resolved, data)
        if not resolved.api_key:
            self._prompt_and_save(resolved)
        return resolved

    def save(self, config: RuntimeConfig) -> Path:
        self._validate(
            config.max_context_tokens,
            config.compact_threshold,
            config.compact_keep_recent_tokens,
        )
        self._write_yaml(self.project_path, self._to_dict(config))
        return self.project_path

    def _apply(self, config: RuntimeConfig, data: dict[str, Any]) -> None:
        config.api_key = self._value(data, "api_key", config.api_key)
        config.base_url = self._value(data, "base_url", config.base_url)
        config.model = self._value(data, "model", config.model)
        config.reasoning_effort = self._value(data, "reasoning_effort", config.reasoning_effort)

        try:
            max_tokens = int(data.get("max_context_tokens", config.max_context_tokens))
            threshold = float(data.get("compact_threshold", config.compact_threshold))
            keep_tokens = int(data.get("compact_keep_recent_tokens", config.compact_keep_recent_tokens))
            self._validate(max_tokens, threshold, keep_tokens)
        except (TypeError, ValueError):
            return
        config.max_context_tokens = max_tokens
        config.compact_threshold = threshold
        config.compact_keep_recent_tokens = keep_tokens

    def _to_dict(self, config: RuntimeConfig) -> dict[str, Any]:
        return {
            "api_key": config.api_key,
            "base_url": config.base_url,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "max_context_tokens": config.max_context_tokens,
            "compact_threshold": config.compact_threshold,
            "compact_keep_recent_tokens": config.compact_keep_recent_tokens,
        }

    def _prompt_and_save(self, config: RuntimeConfig) -> None:
        self.output_fn("未找到 InnoAgent 模型配置。")
        api_key = self.input_fn("API Key: ").strip()
        while not api_key:
            self.output_fn("API Key 不能为空。")
            api_key = self.input_fn("API Key: ").strip()

        config.api_key = api_key
        config.base_url = self.input_fn(f"Base URL [{config.base_url}]: ").strip() or config.base_url
        config.model = self.input_fn(f"Model [{config.model}]: ").strip() or config.model
        config.reasoning_effort = "none"
        self.save(config)

    @staticmethod
    def _value(data: dict[str, Any], key: str, fallback: str = "") -> str:
        value = str(data.get(key) or "").strip()
        return value or fallback

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_yaml(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    @staticmethod
    def _validate(max_tokens: int, threshold: float, keep_tokens: int) -> None:
        if max_tokens < 1024:
            raise ValueError("max context tokens must be at least 1024")
        if not 0.01 <= threshold < 1.0:
            raise ValueError("compact threshold must be between 1% and 99%")
        threshold_tokens = int(max_tokens * threshold)
        if keep_tokens < 1 or keep_tokens >= threshold_tokens:
            raise ValueError("kept recent tokens must be below the compact threshold")
