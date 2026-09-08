"""Unified YAML configuration tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from core.runtime.config import ConfigStore, RuntimeConfig


class ConfigStoreTest(unittest.TestCase):
    """Tests for YAML config loading and persistence."""

    def _store(self, tmp: str, inputs: list[str] | None = None) -> ConfigStore:
        project = Path(tmp) / "project"
        project.mkdir(exist_ok=True)
        global_root = Path(tmp) / "global"
        input_iter = iter(inputs or [])
        return ConfigStore(
            project_root=project,
            global_root=global_root,
            input_fn=lambda prompt="": next(input_iter, ""),
            output_fn=lambda _: None,
        )

    def _write_config(self, store: ConfigStore, **values: object) -> Path:
        store.project_path.parent.mkdir(parents=True, exist_ok=True)
        store.project_path.write_text(
            yaml.safe_dump(values, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return store.project_path

    def test_loads_model_and_context_from_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._write_config(
                store,
                api_key="project-key",
                base_url="https://project.example/v1",
                model="project-model",
                max_context_tokens=64000,
                compact_threshold=0.75,
                compact_keep_recent_tokens=8000,
            )

            config = store.load()

            self.assertEqual(config.api_key, "project-key")
            self.assertEqual(config.model, "project-model")
            self.assertEqual(config.max_context_tokens, 64000)
            self.assertEqual(config.compact_threshold_tokens, 48000)
            self.assertEqual(config.compact_keep_recent_tokens, 8000)

    def test_uses_global_when_project_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.global_path.parent.mkdir(parents=True, exist_ok=True)
            store.global_path.write_text(
                yaml.safe_dump(
                    {
                        "api_key": "global-key",
                        "model": "global-model",
                        "max_context_tokens": 32000,
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            config = store.load()

            self.assertEqual(config.api_key, "global-key")
            self.assertEqual(config.model, "global-model")
            self.assertEqual(config.max_context_tokens, 32000)

    def test_prompts_and_saves_when_api_key_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, inputs=["prompt-key", "", "", "1"])

            config = store.load()

            self.assertEqual(config.api_key, "prompt-key")
            self.assertTrue(store.project_path.exists())
            saved = yaml.safe_load(store.project_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["api_key"], "prompt-key")

    def test_save_persists_all_persistent_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            config = RuntimeConfig(
                api_key="key",
                model="new-model",
                max_context_tokens=64000,
                compact_threshold=0.75,
                compact_keep_recent_tokens=8000,
            )

            store.save(config)

            saved = yaml.safe_load(store.project_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["api_key"], "key")
            self.assertEqual(saved["model"], "new-model")
            self.assertEqual(saved["max_context_tokens"], 64000)
            self.assertEqual(saved["compact_threshold"], 0.75)
            self.assertEqual(saved["compact_keep_recent_tokens"], 8000)


if __name__ == "__main__":
    unittest.main()
