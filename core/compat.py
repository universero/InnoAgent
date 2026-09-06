"""Small compatibility helpers that must run before LangGraph imports."""

from __future__ import annotations

import warnings

from langchain_core._api.deprecation import (
    LangChainDeprecationWarning,
    LangChainPendingDeprecationWarning,
)


def silence_langgraph_deprecations() -> None:
    """Suppress LangChain pending-deprecation noise emitted at import time."""
    warnings.filterwarnings("ignore", category=LangChainDeprecationWarning)
    warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
