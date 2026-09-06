"""Deterministic model used only by tests.

Production runtime no longer ships a rule-based model.  These tests inject this
fake implementation so graph, tool, guardrail, memory and CLI behaviour can be
verified without calling a remote API.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from core.llm import BaseModelClient, ModelDecision, ToolCallDecision


class FakeModel(BaseModelClient):
    """Deterministic test model implementing simple file-operation intents."""

    def respond(
        self,
        context: str,
        tool_schemas: list[dict[str, Any]],
        state: dict[str, Any] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
    ) -> ModelDecision:
        """Return a deterministic decision for test scenarios."""
        if on_token and state:
            on_token(state.get("response", ""))
        state = state or {}
        user_input = str(state.get("user_input", ""))
        goal = state.get("goal")
        plan = state.get("plan")
        tasks = state.get("tasks") or []
        tool_results = state.get("tool_results") or []

        if goal and not plan:
            return ModelDecision(action="planning", message="根据目标创建执行计划")

        if plan:
            pending = [task for task in tasks if task.get("status") not in {"done", "skipped"}]
            if not pending:
                return self._finish(tool_results, goal=goal)
            task = pending[0]
            last_result = tool_results[-1] if tool_results else None
            effective_goal = " ".join(part for part in [goal, user_input] if part)
            if (
                last_result
                and last_result.get("status") == "success"
                and last_result.get("tool_name") != "task"
            ):
                return ModelDecision(
                    action="tool_use",
                    message=f"完成任务 {task.get('title')}",
                    tool_calls=[
                        ToolCallDecision(
                            name="task",
                            arguments={"task_id": task["task_id"], "status": "done"},
                        )
                    ],
                )
            if "验证" in task.get("title", ""):
                return self._verification_call(effective_goal)
            return self._goal_tool_call(effective_goal)

        if tool_results:
            return self._finish(tool_results, goal=None)
        return self._direct_tool_call(user_input)

    def _finish(self, tool_results: list[dict], goal: str | None) -> ModelDecision:
        """Create a finish decision from the latest tool result."""
        if tool_results:
            last = tool_results[-1]
            if last.get("status") != "success":
                return ModelDecision(
                    action="finish",
                    message=f"执行未成功：{last.get('output', 'unknown error')}",
                )
            summary = last.get("output", "已完成")
        else:
            summary = "已完成"
        prefix = f"目标已完成：{goal}" if goal else "执行结果："
        return ModelDecision(action="finish", message=f"{prefix}\n{summary}")

    def _direct_tool_call(self, text: str) -> ModelDecision:
        """Map simple natural-language commands to one tool call."""
        lower = text.lower()
        if any(word in lower for word in ("读取", "read", "查看")):
            path = self._extract_path(text) or "README.md"
            return ModelDecision(
                action="tool_use",
                tool_calls=[ToolCallDecision(name="read", arguments={"path": path})],
            )
        if any(word in lower for word in ("列出", "list", "ls")):
            return ModelDecision(
                action="tool_use",
                tool_calls=[ToolCallDecision(name="ls", arguments={"path": "."})],
            )
        if any(word in lower for word in ("搜索", "grep", "查找")):
            return ModelDecision(
                action="tool_use",
                tool_calls=[
                    ToolCallDecision(
                        name="grep",
                        arguments={"pattern": self._extract_query(text), "path": "."},
                    )
                ],
            )
        if any(word in lower for word in ("写", "创建", "write")):
            path = self._extract_path(text) or "output.txt"
            content = self._extract_content(text) or text
            return ModelDecision(
                action="tool_use",
                tool_calls=[
                    ToolCallDecision(name="write", arguments={"path": path, "content": content})
                ],
            )
        return ModelDecision(action="finish", message="无法自动判断该请求。")

    def _goal_tool_call(self, goal: str) -> ModelDecision:
        """Map a goal to the tool most likely needed."""
        lower = goal.lower()
        if any(word in lower for word in ("读取", "read", "查看")):
            return ModelDecision(
                action="tool_use",
                tool_calls=[
                    ToolCallDecision(
                        name="read",
                        arguments={"path": self._extract_path(goal) or "README.md"},
                    )
                ],
            )
        if any(word in lower for word in ("列出", "list", "ls")):
            return ModelDecision(
                action="tool_use",
                tool_calls=[ToolCallDecision(name="ls", arguments={"path": "."})],
            )
        if any(word in lower for word in ("搜索", "grep", "查找")):
            return ModelDecision(
                action="tool_use",
                tool_calls=[
                    ToolCallDecision(
                        name="grep",
                        arguments={"pattern": self._extract_query(goal), "path": "."},
                    )
                ],
            )
        if any(word in lower for word in ("写", "创建", "write", "文件")):
            path = self._extract_path(goal) or "output.txt"
            content = self._extract_content(goal) or "generated by InnoAgent"
            return ModelDecision(
                action="tool_use",
                tool_calls=[
                    ToolCallDecision(name="write", arguments={"path": path, "content": content})
                ],
            )
        return ModelDecision(action="finish", message="无法自动执行该目标。")

    def _verification_call(self, goal: str) -> ModelDecision:
        """Choose a read or ls verification action."""
        path = self._extract_path(goal) or "README.md"
        if any(word in goal.lower() for word in ("目录", "列出", "ls")):
            return ModelDecision(
                action="tool_use",
                tool_calls=[ToolCallDecision(name="ls", arguments={"path": "."})],
            )
        return ModelDecision(
            action="tool_use",
            tool_calls=[ToolCallDecision(name="read", arguments={"path": path})],
        )

    @staticmethod
    def _extract_path(text: str) -> str | None:
        """Extract a filename or path from text."""
        match = re.search(r"(?:[./]?[\w.-]+(?:/[\w.-]+)+)", text)
        if match:
            return match.group(0)
        match = re.search(r"([\w.-]+\.(?:py|txt|md|json|yaml|yml|toml))", text)
        return match.group(1) if match else None

    @staticmethod
    def _extract_query(text: str) -> str:
        """Extract a grep query from text."""
        match = re.search(r"(?:搜索|grep|查找)\s+([^\s，。]+)", text)
        return match.group(1) if match else "TODO"

    @staticmethod
    def _extract_content(text: str) -> str | None:
        """Extract write content or a print expression from text."""
        match = re.search(r"打印\s*(.+)", text)
        if match:
            code = match.group(1).strip()
            return f'print("{code}")' if not code.startswith("print") else code
        match = re.search(r"(?:内容|写入)[：:\s]+(.+)", text)
        if match:
            return match.group(1).strip()
        match = re.search(r"创建\s+[\w./-]+\s*(?:并|，|,)?\s*(.+)", text)
        if match and match.group(1).strip():
            return match.group(1).strip()
        return None
