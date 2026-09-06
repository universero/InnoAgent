"""Goal completion evaluation."""

from __future__ import annotations

from core.reflection.schemas import ReflectionInput, ReflectionResult


def evaluate_goal(input_data: ReflectionInput) -> ReflectionResult:
    """Evaluate completion against plan/task/tool evidence.

    This is a conservative evaluator.  It considers a goal complete only when
    the model finished, no recent errors remain, all known tasks are done, and
    the response is not empty.  A production deployment can replace this
    function with a model-backed evaluator without changing the runtime contract.
    """
    missing: list[str] = []
    evidence: list[str] = []

    if not input_data.goal.strip():
        return ReflectionResult(
            complete=True,
            confidence=1.0,
            summary="No goal was set; no reflection required.",
        )

    tasks = input_data.tasks or []
    if tasks:
        incomplete = [task for task in tasks if task.get("status") not in {"done", "skipped"}]
        done = [task for task in tasks if task.get("status") == "done"]
        if incomplete:
            missing.append(f"{len(incomplete)} task(s) are not done")
        evidence.append(f"{len(done)}/{len(tasks)} tasks done")
    else:
        missing.append("no plan tasks are available to verify the goal")

    if input_data.errors:
        missing.append(f"{len(input_data.errors)} unresolved error(s) remain")
    else:
        evidence.append("no unresolved tool errors")

    if not input_data.response.strip():
        missing.append("model finished without a response")
    else:
        evidence.append("model produced a final response")

    complete = not missing
    confidence = 0.9 if complete else max(0.2, 1.0 - 0.25 * len(missing))
    summary = "Goal complete." if complete else "Goal not yet complete."
    feedback = (
        ""
        if complete
        else (
            "当前结果还不能满足目标。请优先处理以下缺口，并在必要时更新计划："
            + "；".join(missing)
            + "。"
        )
    )
    return ReflectionResult(
        complete=complete,
        confidence=round(confidence, 2),
        summary=summary,
        feedback=feedback,
        missing_conditions=missing,
        evidence=evidence,
    )
