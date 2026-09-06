"""Reflection domain models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReflectionResult(BaseModel):
    """Outcome produced by goal reflection."""
    complete: bool
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = ""
    feedback: str = ""
    missing_conditions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class ReflectionInput(BaseModel):
    """Input supplied to the reflection evaluator."""
    goal: str
    response: str = ""
    plan: dict | None = None
    tasks: list[dict] = Field(default_factory=list)
    tool_results: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
