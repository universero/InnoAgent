"""Model credential and endpoint configuration loading.

Startup order:

1. ``<project>/.innoagent/config.json``
2. ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``
3. ``~/.innoagent/config.json``
4. interactive prompt, with the option to save to project or global config
"""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass
class ModelConfig:
    """Resolved model credentials and endpoint."""
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    reasoning_effort: str = "none"
    source: str = "project"

    def to_dict(self) -> dict[str, str]:
        """Return configuration fields for JSON persistence."""
        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }


class ModelConfigLoader:
    """Load model config from files, environment, or the user."""

    def __init__(
            self,
            project_root: str | Path = ".",
            input_fn: Callable[[str], str] | None = None,
            output_fn: Callable[[str], None] | None = None,
    ) -> None:
        """读取项目或全局的配置"""
        self.project_root = Path(project_root).expanduser().resolve()  # 项目目录
        self.project_config_path = self.project_root / ".innoagent" / "config.json"  # 项目级别配置文件
        self.global_root = (Path.home() / ".innoagent")  # 全局根目录
        self.global_config_path = self.global_root / "config.json"  # 全局配置文件
        self.input_fn = input_fn or input
        self.output_fn = output_fn or print

    def load(self) -> ModelConfig:
        """返回优先级最高的配置"""
        project_config = self._read_file(self.project_config_path, "project")
        if project_config:
            return project_config

        env_config = self._from_environment()
        if env_config:
            return env_config

        global_config = self._read_file(self.global_config_path, "global")
        if global_config:
            return global_config

        return self._prompt_and_save()

    def _read_file(self, path: Path, source: str) -> ModelConfig | None:
        """Load and validate a config file."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None

        api_key = str(data.get("api_key") or data.get("key") or "").strip()
        if not api_key:
            return None
        base_url = str(data.get("base_url") or DEFAULT_BASE_URL).strip()
        model = str(data.get("model") or DEFAULT_MODEL).strip()
        reasoning_effort = str(data.get("reasoning_effort") or "none").strip()
        return ModelConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
            reasoning_effort=reasoning_effort,
            source=source,
        )

    def _from_environment(self) -> ModelConfig | None:
        """Build config from OpenAI environment variables."""
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        return ModelConfig(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
            model=os.getenv("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            reasoning_effort=os.getenv("INNOAGENT_REASONING_EFFORT", "none").strip() or "none",
            source="environment",
        )

    def _prompt_and_save(self) -> ModelConfig:
        """Ask the user for missing credentials and persist them."""
        self.output_fn("未找到 InnoAgent 模型配置。")
        api_key = self._read_secret("API Key: ").strip()
        while not api_key:
            self.output_fn("API Key 不能为空。")
            api_key = self._read_secret("API Key: ").strip()

        base_url = self.input_fn(f"Base URL [{DEFAULT_BASE_URL}]: ").strip()
        if not base_url:
            base_url = DEFAULT_BASE_URL

        model = self.input_fn(f"Model [{DEFAULT_MODEL}]: ").strip()
        if not model:
            model = DEFAULT_MODEL

        self.output_fn("保存到：[1] 当前项目 .innoagent  [2] 全局 ~/.innoagent")
        choice = self.input_fn("选择 1 或 2：").strip().lower()
        if choice in {"2", "global", "g"}:
            path = self.global_config_path
            source = "global"
        else:
            path = self.project_config_path
            source = "project"

        config = ModelConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
            reasoning_effort="none",
            source=source,
        )
        self.save(config, scope=source)
        return config

    def _read_secret(self, prompt: str) -> str:
        """Read an API key, hiding input when attached to a terminal."""
        if self.input_fn is input:
            return getpass.getpass(prompt)
        return self.input_fn(prompt)

    def save(self, config: ModelConfig, scope: str = "project") -> Path:
        """将模型配置写入项目或全局配置文件。"""
        path = self.project_config_path if scope != "global" else self.global_config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, object] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                pass
        existing.update(config.to_dict())
        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
