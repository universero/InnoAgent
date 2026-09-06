"""Tool search for future tool-heavy configurations."""

from __future__ import annotations

from core.tool.base import ToolContext
from core.tool.registry import ToolRegistry


def search_tools(
    query: str,
    registry: ToolRegistry,
    context: ToolContext | None = None,
    limit: int = 5,
) -> list[dict[str, str]]:
    """Return tool names and descriptions relevant to ``query``.

    The first implementation uses a deliberately simple keyword match.  When
    the tool inventory grows, this function can be replaced with embeddings
    without changing the runtime contract.
    """
    limit = max(1, limit)
    terms = [term.lower() for term in query.split() if term]
    if not terms:
        return [
            {"name": tool.name, "description": tool.dynamic_description(context)}
            for tool in registry.list_tools()[:limit]
        ]

    scored: list[tuple[int, str, str]] = []
    for tool in registry.list_tools():
        description = tool.dynamic_description(context)
        haystack = f"{tool.name} {description}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, tool.name, description))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"name": name, "description": desc} for _, name, desc in scored[:limit]]
