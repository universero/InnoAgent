"""第三方依赖的兼容处理。"""

from __future__ import annotations

import warnings

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning


def silence_langgraph_deprecations() -> None:
    """屏蔽 LangGraph 依赖在导入阶段产生的已知 pending 警告。"""
    warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
