from typing import Literal, Optional

from pydantic import BaseModel, Field


class Fact(BaseModel):
    id: str
    kind: Literal["decision", "requirement", "constraint"]
    text: str
    source_refs: list[str] = Field(default_factory=list)
    owner: Optional[str] = None
    deadline_text: Optional[str] = None
    corrected: bool = False


class Flag(BaseModel):
    id: str
    kind: Literal["missing", "conflict"]
    text: str
    source_refs: list[str] = Field(default_factory=list)
    related_fact_ids: list[str] = Field(default_factory=list)


class IntakeOutput(BaseModel):
    facts: list[Fact]
    flags: list[Flag] = Field(default_factory=list)


class Task(BaseModel):
    id: str
    title: str
    owner: Optional[str] = None
    owner_basis: Literal["stated", "recommended", "unassigned"] = "unassigned"
    deadline: Optional[str] = None
    deadline_basis: Literal["stated", "recommended", "none"] = "none"
    depends_on: list[str] = Field(default_factory=list)
    supported_by: list[str] = Field(default_factory=list)
    basis: Literal["supported", "recommendation"] = "recommendation"
    rationale: str = ""


class Question(BaseModel):
    question: str
    related: list[str] = Field(default_factory=list)


class PlanOutput(BaseModel):
    summary: str = ""
    tasks: list[Task]
    unresolved_questions: list[Question] = Field(default_factory=list)
    changes_made: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    id: str = ""
    task_id: Optional[str] = None
    rule_id: Optional[str] = None
    severity: Literal["blocking", "minor"] = "blocking"
    problem: str
    correction: str
    source: Literal["review-agent", "rule-engine"] = "review-agent"


class ReviewOutput(BaseModel):
    verdict: Literal["pass", "fail"]
    issues: list[Issue] = Field(default_factory=list)
    notes: str = ""


class PlanningInput(BaseModel):
    planning_start: str
    facts: list[Fact]
    open_flags: list[Flag] = Field(default_factory=list)
    resolutions: list[dict] = Field(default_factory=list)
    rules: dict
    previous_plan: Optional[PlanOutput] = None
    review_feedback: list[Issue] = Field(default_factory=list)


class ReviewInput(BaseModel):
    transcript: list[dict]
    facts: list[Fact]
    resolutions: list[dict] = Field(default_factory=list)
    rules: dict
    plan: PlanOutput


class FeedbackMessage(BaseModel):
    round: int
    issues: list[Issue]
