"""Small decorators that make tool policy explicit.

Tools are registered with :class:`ToolRegistry` after class creation.  The
decorators do not perform the actual guardrail checks; that remains the
registry's responsibility so all policies run through a single path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from core.tool.base import BaseTool
from core.tool.registry import tool_registry


T = TypeVar("T", bound=type[BaseTool])


def tool(cls: T) -> T:
    """Register a BaseTool subclass."""
    tool_registry.register(cls())
    return cls


def write_tool(cls: T) -> T:
    """Mark a tool as a write operation and register it."""
    cls.is_write = True
    tool_registry.register(cls())
    return cls


def confirm_write(cls: T) -> T:
    """Mark a tool as requiring confirmation in ask mode."""
    cls.is_write = True
    cls.requires_confirmation = True
    tool_registry.register(cls())
    return cls


def no_register(cls: T) -> T:
    """Mark a tool class without registering it (used by extensions)."""
    return cls


def is_builtin_tool(cls: type[BaseTool]) -> bool:
    """Return whether a class inherits from BaseTool."""
    return issubclass(cls, BaseTool)
