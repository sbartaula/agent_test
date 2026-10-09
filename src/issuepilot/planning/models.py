from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    description: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list, description="Likely files to touch")
    kind: Literal["investigate", "edit", "test", "docs"] = "edit"


class Plan(BaseModel):
    summary: str = Field(min_length=1)
    root_cause_hypothesis: str = ""
    steps: list[PlanStep] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    test_strategy: str = ""


class Evidence(BaseModel):
    file: str
    lines: str = ""
    why: str


class Analysis(BaseModel):
    """Read-only investigation result (Observe mode)."""

    root_cause: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    proposed_fix: str = ""
    confidence: Literal["low", "medium", "high"] = "low"
    open_questions: list[str] = Field(default_factory=list)
