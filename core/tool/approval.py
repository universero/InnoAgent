"""Typed approval protocol shared by runtime, session replay, and UI."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from core.tool.base import ToolCall


ApprovalDecision = Literal["allow_once", "allow_always", "deny"]
VALID_APPROVAL_DECISIONS: tuple[ApprovalDecision, ...] = (
    "allow_once",
    "allow_always",
    "deny",
)


class ApprovalOption(BaseModel):
    """Stable decision value and user-facing copy."""

    value: ApprovalDecision
    label: str
    description: str


def default_approval_options() -> list[ApprovalOption]:
    return [
        ApprovalOption(
            value="allow_once",
            label="Allow once",
            description="Run this operation once",
        ),
        ApprovalOption(
            value="allow_always",
            label="Always allow in this workspace",
            description="Remember this permission for the current workspace",
        ),
        ApprovalOption(
            value="deny",
            label="Deny",
            description="Continue without running this operation",
        ),
    ]


class ApprovalRequest(BaseModel):
    """Serializable pause state for one or more tool calls."""

    request_id: str = Field(default_factory=lambda: uuid4().hex)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    calls: list[ToolCall] = Field(default_factory=list, min_length=1)
    deferred_calls: list[ToolCall] = Field(default_factory=list)
    reason: str = ""
    options: list[ApprovalOption] = Field(
        default_factory=default_approval_options,
        min_length=1,
    )

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_payload(cls, value: Any) -> Any:
        """Normalize checkpoint and event payloads written by older releases."""
        if not isinstance(value, dict):
            return value
        upgraded = dict(value)
        metadata = upgraded.get("metadata")
        if not upgraded.get("arguments") and isinstance(metadata, dict):
            upgraded["arguments"] = dict(metadata.get("arguments") or {})
        calls = upgraded.get("calls")
        if not calls and upgraded.get("tool_name"):
            upgraded["calls"] = [
                {
                    "call_id": str(upgraded.get("call_id") or ""),
                    "name": str(upgraded["tool_name"]),
                    "arguments": dict(upgraded.get("arguments") or {}),
                }
            ]
        if not upgraded.get("tool_name") and upgraded.get("calls"):
            first = upgraded["calls"][0]
            if isinstance(first, dict):
                upgraded["tool_name"] = str(first.get("name") or "")
                upgraded["arguments"] = dict(first.get("arguments") or {})
        return upgraded

    @field_validator("options", mode="before")
    @classmethod
    def _upgrade_legacy_options(cls, value: Any) -> Any:
        if not isinstance(value, list) or not value or not all(
            isinstance(item, str) for item in value
        ):
            return value
        defaults = {item.value: item for item in default_approval_options()}
        upgraded = [defaults[item] for item in value if item in defaults]
        if not upgraded:
            raise ValueError("approval request has no supported options")
        return upgraded

    @classmethod
    def from_calls(
        cls,
        calls: list[dict[str, Any]],
        *,
        deferred_calls: list[dict[str, Any]] | None = None,
        reason: str = "",
    ) -> "ApprovalRequest":
        parsed = [ToolCall.model_validate(call) for call in calls]
        if not parsed:
            raise ValueError("approval request requires at least one tool call")
        first = parsed[0]
        return cls(
            tool_name=first.name,
            arguments=first.arguments,
            calls=parsed,
            deferred_calls=[
                ToolCall.model_validate(call) for call in (deferred_calls or [])
            ],
            reason=reason,
        )

    def accepts(self, decision: str) -> bool:
        return any(option.value == decision for option in self.options)

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def parse_approval_decision(value: str) -> ApprovalDecision | None:
    """Parse CLI compatibility aliases without weakening runtime validation."""
    normalized = value.strip().lower()
    if normalized.startswith("/approve "):
        normalized = normalized.removeprefix("/approve ").strip()
    aliases: dict[str, ApprovalDecision] = {
        "1": "allow_once",
        "once": "allow_once",
        "允许一次": "allow_once",
        "allow_once": "allow_once",
        "2": "allow_always",
        "always": "allow_always",
        "一直允许": "allow_always",
        "allow_always": "allow_always",
        "3": "deny",
        "deny": "deny",
        "拒绝": "deny",
    }
    return aliases.get(normalized)
