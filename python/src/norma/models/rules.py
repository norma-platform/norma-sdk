# Modified for Norma.
"""Hosted Rule definitions and individual check results."""

from typing import Literal

from pydantic import ConfigDict, Field

from .actions import ActionDecision
from .agent import JSONObject, JSONValue
from .base import BaseModel
from .semantic import CheckOutcome, SemanticObservation
from .turn import FinishFinding


class SteeringContext(BaseModel):
    message: str


class RuleAction(BaseModel):
    """Hosted Rule response when its condition matches."""

    model_config = ConfigDict(extra="forbid")
    decision: ActionDecision
    steering_context: SteeringContext | None = None


class RuleScope(BaseModel):
    """Select the hosted checkpoints to which a Rule applies."""

    model_config = ConfigDict(extra="forbid")
    step_types: list[str] | None = None
    step_names: list[str] | None = None
    step_name_regex: str | None = None
    stages: list[Literal["pre", "post"]] | None = None


class RuleDefinition(BaseModel):
    """A hosted Rule definition. Local execution and unknown options are rejected."""

    model_config = ConfigDict(extra="forbid")
    execution: Literal["server"] = "server"
    mode: Literal["enforce", "observe"] = Field(
        default="enforce", exclude_if=lambda value: value == "enforce",
    )
    enabled: bool = True
    condition: JSONObject
    action: RuleAction
    scope: RuleScope | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class EvaluatorResult(BaseModel):
    """Evidence from a completed, failed, or unresolved hosted check."""

    matched: bool
    confidence: float = Field(ge=0, le=1)
    message: str | None = None
    metadata: dict[str, JSONValue] | None = None
    finish_findings: list[FinishFinding] = Field(default_factory=list)
    error: str | None = None
    outcome: CheckOutcome | None = None
    semantic_observation: SemanticObservation | None = None
    failed_condition_ids: list[str] = Field(default_factory=list)
    unresolved_evidence_ids: list[str] = Field(default_factory=list)
    unresolved_action: ActionDecision | None = None


class RuleMatch(BaseModel):
    """One Rule's result; a check identity is not proof an action executed."""

    rule_id: int
    rule_name: str
    check_id: str | None = None
    status: Literal["matched", "not_matched", "error", "unresolved"]
    action: ActionDecision
    mode: Literal["enforce", "observe"] = Field(
        default="enforce", exclude_if=lambda value: value == "enforce",
    )
    effective_action: ActionDecision | None = None
    result: EvaluatorResult
    steering_context: SteeringContext | None = None
