"""Plan tool guardrail."""

from __future__ import annotations

from typing import Any

from core.guardrails.base import BaseGuardrail, GuardrailDecision
from core.tool.base import BaseTool, ToolContext


class PlanGuard(BaseGuardrail):
    """Reject malformed Plan tool invocations before planning starts."""

    name = "plan"

    def before(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> GuardrailDecision:
        """Reject Plan tool invocations without a non-empty goal."""
        if tool.name != "plan":
            return GuardrailDecision()
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return GuardrailDecision(
                allowed=False,
                status="blocked",
                reason="plan tool requires a non-empty goal",
            )
        return GuardrailDecision()
