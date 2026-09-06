"""Model configuration loading tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.runtime.model_config import ModelConfigLoader


class ModelConfigLoaderTest(unittest.TestCase):
    """Tests for model config precedence and interactive saving."""

    def _loader(self, tmp: str, inputs: list[str] | None = None, outputs: list[str] | None = None):
        """Build a test loader with isolated paths."""
        project = Path(tmp) / "project"
        project.mkdir(exist_ok=True)
        global_root = Path(tmp) / "global"
        input_iter = iter(inputs or [])
        output_list = outputs or []
        return ModelConfigLoader(
            project_root=project,
            global_root=global_root,
            input_fn=lambda prompt="": next(input_iter, ""),
            output_fn=output_list.append,
        )

    def _write_config(self, root: Path, name: str = ".innoagent/config.json", **values: str) -> Path:
        """Write a config file and return its path."""
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values), encoding="utf-8")
        return path

    def test_prefers_project_config(self) -> None:
        """Verify project config wins over environment."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self._write_config(
                project,
                api_key="project-key",
                base_url="https://project.example/v1",
                model="project-model",
            )
            with patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "", "OPENAI_BASE_URL": "", "OPENAI_MODEL": ""},
            ):
                config = self._loader(tmp).load()
            self.assertEqual(config.api_key, "project-key")
            self.assertEqual(config.source, "project")

    def test_uses_environment_when_project_missing(self) -> None:
        """Verify environment variables are used as fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "env-key",
                    "OPENAI_BASE_URL": "https://env.example/v1",
                    "OPENAI_MODEL": "env-model",
                },
            ):
                config = self._loader(tmp).load()
            self.assertEqual(config.api_key, "env-key")
            self.assertEqual(config.source, "environment")

    def test_uses_global_when_no_env_or_project(self) -> None:
        """Verify global config is used after environment."""
        with tempfile.TemporaryDirectory() as tmp:
            global_root = Path(tmp) / "global"
            global_root.mkdir()
            (global_root / "config.json").write_text(
                json.dumps(
                    {
                        "api_key": "global-key",
                        "base_url": "https://global.example/v1",
                        "model": "global-model",
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "", "OPENAI_BASE_URL": "", "OPENAI_MODEL": ""},
            ):
                config = self._loader(tmp).load()
            self.assertEqual(config.api_key, "global-key")
            self.assertEqual(config.source, "global")

    def test_prompt_saves_to_global(self) -> None:
        """Verify interactive prompt can save to global."""
        with tempfile.TemporaryDirectory() as tmp:
            outputs: list[str] = []
            loader = self._loader(
                tmp,
                inputs=["prompt-key", "", "", "2"],
                outputs=outputs,
            )
            with patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "", "OPENAI_BASE_URL": "", "OPENAI_MODEL": ""},
            ):
                config = loader.load()
            self.assertEqual(config.source, "global")
            self.assertTrue(loader.global_config_path.exists())

    def test_prompt_saves_to_project_by_default(self) -> None:
        """Verify interactive prompt saves to project by default."""
        with tempfile.TemporaryDirectory() as tmp:
            loader = self._loader(tmp, inputs=["prompt-key", "", "", "1"])
            with patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "", "OPENAI_BASE_URL": "", "OPENAI_MODEL": ""},
            ):
                config = loader.load()
            self.assertEqual(config.source, "project")
            self.assertTrue(loader.project_config_path.exists())


if __name__ == "__main__":
    unittest.main()
