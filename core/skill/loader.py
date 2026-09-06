"""Discover Agent Skills and load their bodies on demand."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from core.session.compression import estimate_tokens


SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class Skill(BaseModel):
    """Metadata and lazily loaded content for one skill."""

    name: str
    description: str
    path: str
    content: str | None = None


class SkillLoader:
    """Discover project and user skills using first-match precedence."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        home: str | Path | None = None,
        max_skill_tokens: int = 5000,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.home = Path(home).expanduser().resolve() if home else Path.home()
        self.max_skill_tokens = max_skill_tokens

    @property
    def roots(self) -> list[Path]:
        return [
            self.workspace_root / ".innoagent" / "skills",
            self.workspace_root / ".agents" / "skills",
            self.home / ".innoagent" / "skills",
            self.home / ".agents" / "skills",
        ]

    def discover(self) -> list[Skill]:
        found: dict[str, Skill] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                skill = self._metadata(path)
                if skill and skill.name not in found:
                    found[skill.name] = skill
        return list(found.values())

    def load(self, name: str) -> Skill:
        for skill in self.discover():
            if skill.name != name:
                continue
            text = Path(skill.path).read_text(encoding="utf-8", errors="replace")
            if estimate_tokens(text) > self.max_skill_tokens:
                text = text[: self.max_skill_tokens * 4] + "\n\n[skill content truncated]"
            return skill.model_copy(update={"content": text})
        raise KeyError(f"unknown skill: {name}")

    def prompt_index(self) -> str:
        skills = self.discover()
        if not skills:
            return ""
        lines = ["Available skills (load with the skill tool when relevant):"]
        for skill in skills:
            lines.append(f"- {skill.name}: {skill.description} ({skill.path})")
        return "\n".join(lines)

    def _metadata(self, path: Path) -> Skill | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        frontmatter = _parse_frontmatter(text)
        name = frontmatter.get("name", "").strip()
        description = frontmatter.get("description", "").strip()
        if not name or not description or not SKILL_NAME_PATTERN.fullmatch(name):
            return None
        return Skill(name=name, description=description, path=str(path.resolve()))


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the small scalar subset needed by Agent Skill frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return values
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return {}
