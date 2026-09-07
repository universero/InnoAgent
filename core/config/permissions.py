"""Repository-local permission rules enforced by the runtime."""

from __future__ import annotations

import json
import os
import shlex
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


PermissionAction = Literal["allow", "deny"]
PermissionScope = Literal["exact", "path", "shell", "command", "command_prefix"]


class PermissionRule(BaseModel):
    """A versioned workspace rule with an explicit matching scope."""

    tool: str
    action: PermissionAction = "allow"
    scope: PermissionScope = "exact"
    path: str | None = None
    command_text: str | None = None
    command: list[str] = Field(default_factory=list)
    # v1 compatibility only; new approvals never create prefix rules implicitly.
    command_prefix: list[str] = Field(default_factory=list)
    arguments: dict[str, Any] | None = None


class PermissionStore:
    """Load and persist permission rules under the current workspace."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.path = self.workspace_root / ".innoagent" / "permissions.json"

    def rules(self) -> list[PermissionRule]:
        rules, _ = self._load_rules()
        return rules

    def _load_rules(self) -> tuple[list[PermissionRule], bool]:
        """Return parsed rules and whether an existing policy file was invalid."""
        if not self.path.exists():
            return [], False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return [], True
        if not isinstance(raw, dict) or raw.get("version", 1) not in {1, 2, 3}:
            return [], True
        values = raw.get("rules", [])
        if not isinstance(values, list):
            return [], True
        rules: list[PermissionRule] = []
        invalid = False
        for value in values:
            try:
                rules.append(PermissionRule.model_validate(self._upgrade_rule(value)))
            except Exception:
                invalid = True
        return rules, invalid

    def decision(self, tool: str, arguments: dict[str, Any]) -> PermissionAction | None:
        """Return the first matching decision using deny-before-allow precedence."""
        rules, invalid = self._load_rules()
        if invalid:
            # 已存在但不可解释的策略文件不能退化成“没有限制”。
            return "deny"
        matching = [rule for rule in rules if self._matches(rule, tool, arguments)]
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
        rules, invalid = self._load_rules()
        if invalid:
            return ["deny: all [invalid permission file]"]
        lines: list[str] = []
        for rule in rules:
            target = rule.path or rule.command_text or " ".join(
                rule.command or rule.command_prefix
            )
            if not target and rule.arguments is not None:
                target = json.dumps(rule.arguments, ensure_ascii=False, sort_keys=True)
            lines.append(
                f"{rule.action}: {rule.tool} [{rule.scope}] {target}".rstrip()
            )
        return lines

    def _save(self, rules: list[PermissionRule]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 3, "rules": [rule.model_dump(exclude_none=True) for rule in rules]}
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def _rule_for(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        action: PermissionAction,
    ) -> PermissionRule:
        if tool == "shell":
            return PermissionRule(
                tool=tool,
                action=action,
                scope="shell",
                command_text=str(arguments.get("command") or ""),
            )
        if "path" in arguments:
            path = Path(str(arguments["path"])).expanduser()
            if not path.is_absolute():
                path = self.workspace_root / path
            try:
                relative = path.resolve().relative_to(self.workspace_root)
                stored_path = str(relative) or "."
            except ValueError:
                stored_path = str(path.resolve())
            return PermissionRule(
                tool=tool,
                action=action,
                scope="path",
                path=stored_path,
            )
        return PermissionRule(
            tool=tool,
            action=action,
            scope="exact",
            arguments=arguments,
        )

    def _matches(
        self,
        rule: PermissionRule,
        tool: str,
        arguments: dict[str, Any],
    ) -> bool:
        if rule.tool != tool:
            return False
        if rule.scope == "shell":
            return str(arguments.get("command") or "") == (rule.command_text or "")
        if rule.scope in {"command", "command_prefix"}:
            # 旧版 allow 丢失了引号等 shell 语义，无法安全地继续授权。
            if rule.action == "allow":
                return False
            try:
                command = shlex.split(str(arguments.get("command") or ""))
            except ValueError:
                return False
            if rule.scope == "command":
                return command == rule.command
            # 旧版 prefix 规则保持兼容；新版只在未来显式策略提议时创建。
            return command[: len(rule.command_prefix)] == rule.command_prefix
        if rule.scope == "path" and rule.path is not None:
            raw_path = Path(str(arguments.get("path") or "")).expanduser()
            candidate = raw_path if raw_path.is_absolute() else self.workspace_root / raw_path
            try:
                candidate_text = str(candidate.resolve().relative_to(self.workspace_root)) or "."
            except ValueError:
                candidate_text = str(candidate.resolve())
            return candidate_text == rule.path
        return rule.arguments == arguments

    @staticmethod
    def _upgrade_rule(value: Any) -> Any:
        """Attach explicit legacy scopes before applying v3 matching rules."""
        if not isinstance(value, dict) or value.get("scope"):
            return value
        upgraded = dict(value)
        if upgraded.get("command_prefix"):
            upgraded["scope"] = "command_prefix"
        elif upgraded.get("path") is not None:
            upgraded["scope"] = "path"
        else:
            upgraded["scope"] = "exact"
        return upgraded
