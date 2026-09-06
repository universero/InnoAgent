"""Repository-local permission rules enforced by the runtime."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


PermissionAction = Literal["allow", "deny"]


class PermissionRule(BaseModel):
    """A minimal rule that can be matched without model involvement."""

    tool: str
    action: PermissionAction = "allow"
    path: str | None = None
    command_prefix: list[str] = Field(default_factory=list)
    arguments: dict[str, Any] | None = None


class PermissionStore:
    """Load and persist permission rules under the current workspace."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.path = self.workspace_root / ".innoagent" / "permissions.json"

    def rules(self) -> list[PermissionRule]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        values = raw.get("rules", []) if isinstance(raw, dict) else []
        rules: list[PermissionRule] = []
        for value in values:
            try:
                rules.append(PermissionRule.model_validate(value))
            except Exception:
                continue
        return rules

    def decision(self, tool: str, arguments: dict[str, Any]) -> PermissionAction | None:
        """Return the first matching decision using deny-before-allow precedence."""
        matching = [rule for rule in self.rules() if self._matches(rule, tool, arguments)]
        # deny 必须优先，避免更宽泛的 allow 覆盖显式禁止规则。
        if any(rule.action == "deny" for rule in matching):
            return "deny"
        if any(rule.action == "allow" for rule in matching):
            return "allow"
        return None

    def allow(self, tool: str, arguments: dict[str, Any]) -> PermissionRule:
        rule = self._rule_for(tool, arguments, action="allow")
        existing = self.rules()
        if rule not in existing:
            existing.append(rule)
            self._save(existing)
        return rule

    def deny(self, tool: str, arguments: dict[str, Any]) -> PermissionRule:
        rule = self._rule_for(tool, arguments, action="deny")
        existing = self.rules()
        if rule not in existing:
            existing.append(rule)
            self._save(existing)
        return rule

    def describe(self) -> list[str]:
        lines: list[str] = []
        for rule in self.rules():
            target = rule.path or " ".join(rule.command_prefix)
            if not target and rule.arguments is not None:
                target = json.dumps(rule.arguments, ensure_ascii=False, sort_keys=True)
            lines.append(f"{rule.action}: {rule.tool} {target}".rstrip())
        return lines

    def _save(self, rules: list[PermissionRule]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "rules": [rule.model_dump(exclude_none=True) for rule in rules]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _rule_for(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        action: PermissionAction,
    ) -> PermissionRule:
        if tool == "shell":
            command = str(arguments.get("command") or "")
            try:
                prefix = shlex.split(command)
            except ValueError:
                prefix = [command]
            return PermissionRule(tool=tool, action=action, command_prefix=prefix)
        if "path" in arguments:
            path = Path(str(arguments["path"])).expanduser()
            if not path.is_absolute():
                path = self.workspace_root / path
            try:
                relative = path.resolve().relative_to(self.workspace_root)
                stored_path = str(relative) or "."
            except ValueError:
                stored_path = str(path.resolve())
            return PermissionRule(tool=tool, action=action, path=stored_path)
        return PermissionRule(tool=tool, action=action, arguments=arguments)

    def _matches(
        self,
        rule: PermissionRule,
        tool: str,
        arguments: dict[str, Any],
    ) -> bool:
        if rule.tool != tool:
            return False
        if rule.command_prefix:
            try:
                command = shlex.split(str(arguments.get("command") or ""))
            except ValueError:
                return False
            # 使用 argv 前缀而非字符串 startswith，避免相似命令名误命中。
            return command[: len(rule.command_prefix)] == rule.command_prefix
        if rule.path is not None:
            raw_path = Path(str(arguments.get("path") or "")).expanduser()
            candidate = raw_path if raw_path.is_absolute() else self.workspace_root / raw_path
            try:
                candidate_text = str(candidate.resolve().relative_to(self.workspace_root)) or "."
            except ValueError:
                candidate_text = str(candidate.resolve())
            return candidate_text == rule.path
        return rule.arguments == arguments
