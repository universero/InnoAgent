"""Planning, reflection, and compaction model stages."""

from __future__ import annotations

import json
from typing import Any, Callable, TypedDict

from core.agent.model_stream import ModelStreamConsumer
from core.compat import silence_langgraph_deprecations
from core.event.events import AgentEvent
from core.llm import OpenAICompatibleModel
from core.planning.planner import PlanningService
from core.planning.schemas import Plan, PlanningRequest, PlanStep
from core.planning.tasks import tasks_from_plan
from core.prompts import PLANNING_PROMPT, REFLECTION_PROMPT
from core.reflection.evaluator import evaluate_goal
from core.reflection.schemas import ReflectionInput, ReflectionResult

silence_langgraph_deprecations()

from langgraph.graph import END, START, StateGraph  # noqa: E402


class PlanningStageState(TypedDict, total=False):
    goal: str
    feedback: str | None
    existing_plan: dict[str, Any] | None
    output: dict[str, Any]


class ReflectionStageState(TypedDict, total=False):
    agent_state: dict[str, Any]
    result: ReflectionResult


class StageRunner:
    """Run structured auxiliary model stages with deterministic fallbacks."""

    def __init__(
        self,
        model: Any,
        stream: ModelStreamConsumer,
        emit: Callable[[AgentEvent], None],
    ) -> None:
        self.model = model
        self.stream = stream
        self.emit = emit
        self.last_usage: dict[str, int] = {}
        self.plan_graph = self._build_plan_graph()
        self.reflection_graph = self._build_reflection_graph()

    def _build_plan_graph(self):
        graph = StateGraph(PlanningStageState)
        graph.add_node("generate_plan", self._plan_node)
        graph.add_edge(START, "generate_plan")
        graph.add_edge("generate_plan", END)
        return graph.compile(name="innoagent-planning")

    def _build_reflection_graph(self):
        graph = StateGraph(ReflectionStageState)
        graph.add_node("evaluate_goal", self._reflection_node)
        graph.add_edge(START, "evaluate_goal")
        graph.add_edge("evaluate_goal", END)
        return graph.compile(name="innoagent-reflection")

    def run_plan(
        self,
        goal: str,
        feedback: str | None,
        existing_plan: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """运行 Planning 子图并返回结构化结果。"""
        state = self.plan_graph.invoke(
            {"goal": goal, "feedback": feedback, "existing_plan": existing_plan}
        )
        return dict(state["output"])

    def _plan_node(self, state: PlanningStageState) -> dict[str, Any]:
        goal = str(state.get("goal") or "")
        feedback = state.get("feedback")
        existing_plan = state.get("existing_plan")
        payload = {"goal": goal, "feedback": feedback, "existing_plan": existing_plan}
        text = self.complete(
            PLANNING_PROMPT + "\n\nInput:\n" + json.dumps(payload, ensure_ascii=False, default=str),
            stage="plan",
        )
        warnings: list[str] = []
        try:
            plan, tasks, message = self._parse_plan(text, goal, existing_plan)
        except Exception as exc:
            warnings.append(f"plan model output invalid; deterministic fallback used: {exc}")
            request = PlanningRequest(
                goal=goal,
                feedback=feedback,
                existing_plan=Plan.model_validate(existing_plan) if existing_plan else None,
            )
            output = (
                PlanningService().update_plan(request)
                if request.existing_plan
                else PlanningService().create_plan(request)
            )
            plan, tasks, message = output.plan, output.tasks, output.message
        result = {
            "plan": plan.model_dump(),
            "tasks": [task.model_dump() for task in tasks],
            "message": message,
            "warnings": warnings,
            "usage": dict(self.last_usage),
        }
        self.emit(AgentEvent(type="item.completed", stage="plan", item_type="plan", payload=result))
        return {"output": result}

    def reflect(self, state: dict[str, Any]) -> ReflectionResult:
        """运行 Reflection 子图并返回目标评估。"""
        output = self.reflection_graph.invoke({"agent_state": state})
        return output["result"]

    def _reflection_node(self, graph_state: ReflectionStageState) -> dict[str, Any]:
        state = graph_state.get("agent_state", {})
        reflection_input = ReflectionInput(
            goal=str(state.get("goal") or ""),
            response=str(state.get("response") or ""),
            plan=state.get("plan"),
            tasks=state.get("tasks", []),
            tool_results=state.get("tool_results", []),
            errors=state.get("errors", []),
        )
        text = self.complete(
            REFLECTION_PROMPT
            + "\n\nEvidence:\n"
            + json.dumps(reflection_input.model_dump(), ensure_ascii=False, default=str),
            stage="reflect",
        )
        try:
            result = ReflectionResult.model_validate(parse_json_object(text))
        except Exception:
            result = evaluate_goal(reflection_input)
        self.emit(
            AgentEvent(
                type="item.completed",
                stage="reflect",
                item_type="reflection",
                payload=result.model_dump(),
            )
        )
        return {"result": result}

    def complete(self, prompt: str, *, stage: str) -> str:
        self.last_usage = {}
        if not isinstance(self.model, OpenAICompatibleModel):
            return ""
        batch = self.stream.call(
            prompt,
            {"user_input": prompt},
            stage=stage,
            tool_schemas=[],
        )
        self.last_usage = dict(batch.usage)
        return batch.text

    def _parse_plan(
        self,
        text: str,
        goal: str,
        existing_plan: dict[str, Any] | None,
    ):
        parsed = parse_json_object(text)
        raw_steps = parsed.get("steps") or []
        if not raw_steps:
            raise ValueError("planner returned no steps")
        completed = {
            step.get("title"): step
            for step in (existing_plan or {}).get("steps", [])
            if step.get("status") == "done"
        }
        steps: list[PlanStep] = []
        dependencies: dict[str, list[str]] = {}
        for raw in raw_steps:
            item = {"title": str(raw)} if isinstance(raw, str) else dict(raw)
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            old = completed.get(title)
            steps.append(
                PlanStep(
                    title=title,
                    description=str(item.get("description") or ""),
                    status="done" if old else item.get("status", "pending"),
                    result=old.get("result") if old else None,
                )
            )
            dependencies[title] = [str(value) for value in item.get("depends_on", [])]
        title_to_id = {step.title: step.step_id for step in steps}
        for step in steps:
            step.depends_on = [title_to_id[name] for name in dependencies[step.title] if name in title_to_id]
        plan = Plan(
            goal=goal,
            revision=int((existing_plan or {}).get("revision", 0)) + 1,
            steps=steps,
            rationale=str(parsed.get("summary") or ""),
        )
        PlanningService()._validate(plan)
        tasks = tasks_from_plan(plan)
        return plan, tasks, str(parsed.get("summary") or f"计划已生成，共 {len(steps)} 步")


def parse_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed
