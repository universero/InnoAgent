"""Compatibility import for the unified event-driven runtime."""

from core.agent.react import EventDrivenAgent


class InnoAgentRuntime(EventDrivenAgent):
    """Backward-compatible name for :class:`EventDrivenAgent`."""


__all__ = ["InnoAgentRuntime"]
