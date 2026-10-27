# Modified for Norma.
"""Public decisions expressed as Rules and checks, independent of wire names."""

from typing import Self

from pydantic import Field, model_validator

from .base import BaseModel
from .control_evidence import ControlEvidenceSnapshot, ControlReceipt
from .recovery_feedback import RecoveryFeedback
from .rules import RuleMatch


class Decision(BaseModel):
    """Whether the checked proposal may proceed; never an execution receipt.

    An enforcing failed check or deny/steer result blocks. Observed Rule results
    remain diagnostic. Unresolved evidence uses
    its effective Rule action, falling back to its configured action.
    """

    allowed: bool
    reason: str | None = None
    checks: list[RuleMatch] = Field(default_factory=list)
    execution_contract: str | None = None
    recovery_feedback: list[RecoveryFeedback] | None = None
    control_contract: str | None = Field(default=None, exclude_if=lambda value: value is None)
    agent_generation: str | None = Field(default=None, exclude_if=lambda value: value is None)
    control_resolutions: list[ControlReceipt] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    control_evidence: list[ControlEvidenceSnapshot] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _block_failed_checks(self) -> Self:
        blocking = any(
            check.mode == "enforce"
            and (
                check.status == "error"
                or check.result.error is not None
                or (
                    check.status in {"matched", "unresolved"}
                    and (check.effective_action or check.action) in {"deny", "steer"}
                )
            )
            for check in self.checks
        )
        if blocking and self.allowed:
            object.__setattr__(self, "allowed", False)
        return self

    def __bool__(self) -> bool:
        return self.allowed

    def __str__(self) -> str:
        status = "ALLOWED" if self.allowed else "BLOCKED"
        return f"Norma decision: {status}" + (f" - {self.reason}" if self.reason else "")
