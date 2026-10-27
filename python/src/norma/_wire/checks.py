# Modified for Norma.
"""Check-related models."""

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from ..models.agent import AGENT_NAME_MIN_LENGTH, AGENT_NAME_PATTERN, Step, normalize_agent_name
from ..models.base import BaseModel
from ..models.control_evidence import ControlEvidenceSnapshot, ControlReceipt
from .recovery_feedback import RecoveryFeedback
from .rules import RuleMatch

_TARGET_FIELD = Annotated[
    str | None,
    StringConstraints(min_length=1, max_length=255),
]


class CheckRequest(BaseModel):
    """
    Request model for check analysis.

    Used to analyze agent interactions for safety violations,
    policy compliance, and rule rules.

    Attributes:
        agent_name: Unique identifier of the agent making the request
        step: Step payload for check
        stage: 'pre' (before execution) or 'post' (after execution)
        target_type: Optional opaque target kind. When set together with
            ``target_id``, the server merges rules bound to that target
            into the effective set, in addition to the agent's direct and
            policy-derived rules.
        target_id: Optional opaque target identifier. Required when
            ``target_type`` is set.
    """

    execution_mode: Literal["hosted"] = "hosted"

    agent_name: str = Field(
        ...,
        min_length=AGENT_NAME_MIN_LENGTH,
        pattern=AGENT_NAME_PATTERN,
        description="Identifier of the agent making the check request",
    )
    step: Step = Field(..., description="Agent step payload to evaluate")
    stage: Literal["pre", "post"] = Field(..., description="Check stage: 'pre' or 'post'")
    target_type: _TARGET_FIELD = Field(
        default=None,
        description=(
            "Optional opaque target kind. When set together with target_id, "
            "the server merges rules bound to that target into the "
            "effective set, in addition to the agent's direct and "
            "policy-derived rules."
        ),
    )
    target_id: _TARGET_FIELD = Field(
        default=None,
        description=("Optional opaque target identifier. Required when target_type is set."),
    )

    @model_validator(mode="after")
    def _check_target_pair(self) -> Self:
        if (self.target_type is None) != (self.target_id is None):
            raise ValueError("target_type and target_id must be supplied together.")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "agent_name": "customer-service-bot",
                    "step": {
                        "type": "llm",
                        "name": "support-answer",
                        "input": "What is the customer's credit card number?",
                        "context": {"user_id": "user123", "session_id": "abc123"},
                    },
                    "stage": "pre",
                },
                {
                    "agent_name": "customer-service-bot",
                    "step": {
                        "type": "llm",
                        "name": "support-answer",
                        "input": "What is the customer's credit card number?",
                        "output": "I cannot share sensitive payment information.",
                        "context": {"user_id": "user123", "session_id": "abc123"},
                    },
                    "stage": "post",
                },
                {
                    "agent_name": "customer-service-bot",
                    "step": {
                        "type": "tool",
                        "name": "search_database",
                        "input": {"query": "SELECT * FROM users"},
                        "context": {"user_id": "user123"},
                    },
                    "stage": "pre",
                },
                {
                    "agent_name": "customer-service-bot",
                    "step": {
                        "type": "tool",
                        "name": "search_database",
                        "input": {"query": "SELECT * FROM users"},
                        "output": {"results": []},
                        "context": {"user_id": "user123"},
                    },
                    "stage": "post",
                },
            ]
        }
    }

    @field_validator("agent_name", mode="before")
    @classmethod
    def validate_and_normalize_agent_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))


class CheckResponse(BaseModel):
    """
    Response model from check analysis (server-side).

    This is what the server returns. The SDK may transform this
    into an CheckResult for client convenience.

    Attributes:
        allowed: Native decision field: whether the evaluated Rules permit this checkpoint
        confidence: Aggregate score, not assurance that an action is safe
        reason: Optional explanation for the decision
        matches: List of rules that matched/triggered (if any)
        errors: List of rules that failed during check (if any)
        non_matches: List of rules that were evaluated but did not match (if any)
    """

    execution_contract: str | None = None
    control_contract: Literal["rule-controls-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    agent_generation: str | None = Field(default=None, exclude_if=lambda value: value is None)
    control_resolutions: list[ControlReceipt] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    control_evidence: list[ControlEvidenceSnapshot] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    allowed: bool = Field(
        ..., description="Whether the evaluated Rules permit this checkpoint (native field)"
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Aggregate score (0.0 to 1.0), not a safety guarantee",
    )
    reason: str | None = Field(
        default=None,
        description="Explanation for the decision",
    )
    matches: list[RuleMatch] | None = Field(
        default=None,
        description="List of rules that matched/triggered (if any)",
    )
    errors: list[RuleMatch] | None = Field(
        default=None,
        description="List of rules that failed during check (if any)",
    )
    non_matches: list[RuleMatch] | None = Field(
        default=None,
        description="List of rules that were evaluated but did not match (if any)",
    )
    unresolved: list[RuleMatch] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Rules with insufficient evidence or an ambiguous semantic answer",
    )
    recovery_feedback: list[RecoveryFeedback] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Source-linked semantic failure signals; no automatic corrective execution",
    )


class CheckResult(CheckResponse):
    """
    Client-side result model for check analysis.

    Extends CheckResponse with additional client-side convenience methods.
    This is what SDK users interact with.
    """

    @model_validator(mode="after")
    def _enforce_blocks(self) -> Self:
        """Whether evaluated Rules permit this checkpoint; not an action receipt."""
        blocking = any(
            match.mode == "enforce"
            and (match.effective_action or match.action) in {"deny", "steer"}
            for match in [*(self.matches or []), *(self.unresolved or [])]
        )
        if self.errors or blocking:
            object.__setattr__(self, "allowed", False)
        return self

    def is_confident(self, threshold: float = 0.8) -> bool:
        """
        Check if the result confidence exceeds a threshold.

        Args:
            threshold: Minimum confidence threshold (default: 0.8)

        Returns:
            True if confidence >= threshold
        """
        return self.confidence >= threshold

    def __bool__(self) -> bool:
        """
        Allow boolean check of result.

        Returns:
            True if content is safe, False otherwise
        """
        return self.allowed

    def __str__(self) -> str:
        """
        String representation of the result.

        Returns:
            Human-readable description
        """
        status = "ALLOWED" if self.allowed else "BLOCKED"
        base = f"Norma decision: {status}"
        if self.reason:
            return f"{base} - {self.reason}"
        return base
