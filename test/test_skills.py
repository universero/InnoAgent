"""Progressive Skill discovery tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.skill.loader import SkillLoader


class SkillLoaderTest(unittest.TestCase):
    def test_discovers_metadata_then_loads_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agents" / "skills" / "code-review" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\nname: code-review\ndescription: Review code for correctness.\n---\n\n# Workflow\nInspect tests.",
                encoding="utf-8",
            )
            loader = SkillLoader(tmp, home=Path(tmp) / "home")
            discovered = loader.discover()
            self.assertEqual([(skill.name, skill.content) for skill in discovered], [("code-review", None)])
            loaded = loader.load("code-review")
            self.assertIn("# Workflow", loaded.content or "")
            self.assertIn("code-review", loader.prompt_index())

    def test_project_skill_wins_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            home = Path(tmp) / "home"
            for root, description in [
                (project / ".innoagent" / "skills", "project"),
                (home / ".agents" / "skills", "home"),
            ]:
                path = root / "demo" / "SKILL.md"
                path.parent.mkdir(parents=True)
                path.write_text(f"---\nname: demo\ndescription: {description}\n---\n", encoding="utf-8")
            loader = SkillLoader(project, home=home)
            self.assertEqual(loader.discover()[0].description, "project")


if __name__ == "__main__":
    unittest.main()
