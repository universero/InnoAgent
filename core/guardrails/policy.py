"""Execution-mode policy shared by the CLI and file guardrails."""

from __future__ import annotations

from enum import Enum


# 运行模式
class RunMode(str, Enum):
    AUTO = "auto"
    ASK = "ask"
    READONLY = "readonly"

    @property
    def is_readonly(self):
        return self is RunMode.READONLY

    @property
    def should_confirm_write(self) -> bool:
        return self is RunMode.ASK

    def __str__(self) -> str:
        return str(self.value)
