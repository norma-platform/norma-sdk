# Modified for Norma.
"""Rule receipt wire types. Definitions remain opaque server-owned JSON."""

from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from ..models.actions import ActionDecision, normalize_action
from ..models.agent import JSONValue
from ..models.base import BaseModel
from ..models.turn import FinishFinding
from .semantic import CheckOutcome, SemanticObservation


class RuleDefinition(BaseModel):
    """Only execution locality is interpreted by the hosted client."""

    execution: Literal["server", "sdk"]
    mode: Literal["enforce", "observe"] = Field(
        default="enforce", exclude_if=lambda value: value == "enforce",
    )
    enabled: bool = True
    condition: JSONValue = None
    model_config = {"extra": "allow"}


RuleDefinition = RuleDefinition
RuleDefinitionRuntime = RuleDefinition


class SteeringContext(BaseModel):
    message: str


class EvaluatorResult(BaseModel):
    """Hosted evaluator receipt, distinguishing errors from completed nonmatches.

    An error means the check could not establish a decision. It never grants
    permission to execute; checkpoint enforcement blocks reported errors.
    """

    matched: bool = Field(..., description="Whether the pattern matched")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Evaluator score; not calibrated safety confidence"
    )
    message: str | None = Field(default=None, description="Explanation of the result")
    metadata: dict[str, Any] | None = Field(default=None, description="Additional result metadata")
    finish_findings: list[FinishFinding] = Field(default_factory=list)
    provider_input_tokens: int | None = Field(default=None, ge=0)
    provider_usage_complete: bool = True
    error: str | None = Field(
        default=None,
        description=(
            "Error message if check failed internally. "
            "When set, matched=False is due to error, not actual check."
        ),
    )
    outcome: CheckOutcome | None = Field(default=None, exclude_if=lambda value: value is None)
    semantic_observation: SemanticObservation | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    failed_condition_ids: list[str] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    unresolved_evidence_ids: list[str] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    unresolved_action: ActionDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def error_implies_not_matched(self) -> Self:
        """Reject contradictory receipt fields without interpreting policy."""
        if self.error is not None and self.matched:
            raise ValueError("matched must be False when error is set")
        if self.outcome is not None and self.matched != (self.outcome == "matched"):
            raise ValueError("matched must agree with the explicit outcome")
        if self.error is not None and self.outcome == "unresolved":
            raise ValueError("An infrastructure error cannot be an unresolved evidence outcome")
        return self


class RuleMatch(BaseModel):
    """Represents a rule check result (match, non-match, or error)."""

    check_id: str | None = Field(
        default=None,
        description="Unique ID for this rule execution (assigned to this observation)",
    )
    rule_id: int = Field(..., description="Database ID of the rule")
    rule_name: str = Field(..., description="Name of the rule")
    action: ActionDecision = Field(..., description="Action configured for this rule")
    result: EvaluatorResult = Field(
        ..., description="Evaluator result (confidence, message, metadata)"
    )
    steering_context: SteeringContext | None = Field(
        None, description="Steering context for steer actions if configured"
    )
    mode: Literal["enforce", "observe"] = Field(
        default="enforce", exclude_if=lambda value: value == "enforce",
    )
    effective_action: ActionDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Action applied to an unresolved outcome, if different from ordinary matching",
    )

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action_value(cls, value: str) -> ActionDecision:
        return normalize_action(value)
