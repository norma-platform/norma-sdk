# Modified for Norma.
"""Durable operation decisions and separately reported or verified effects."""

import json
from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..models.agent import AGENT_NAME_MIN_LENGTH, AGENT_NAME_PATTERN, Step, normalize_agent_name
from ..models.base import BaseModel
from ..models.control_evidence import ControlReceipt
from .checks import CheckResponse
from .rules import RuleDefinitionRuntime
from .verification import VerificationAssessment


class OperationRequestModel(BaseModel):
    """Reject caller-supplied fields that could impersonate stored outcomes."""

    model_config = ConfigDict(extra="forbid")


class OperationEvidence(OperationRequestModel):
    """Bounded evidence supplied by the caller; it is never treated as verified."""

    id: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2000)
    source: str = Field(min_length=1, max_length=255)


class OperationProposal(OperationRequestModel):
    """Stored proposal, including historical execution modes and error policies."""

    agent_name: str = Field(
        min_length=AGENT_NAME_MIN_LENGTH,
        max_length=255,
        pattern=AGENT_NAME_PATTERN,
    )
    execution_mode: Literal["hosted"] | None = None
    operation_key: str = Field(min_length=1, max_length=128)
    step: Step
    stage: Literal["pre"] = "pre"
    target_type: str | None = Field(default=None, min_length=1, max_length=255)
    target_id: str | None = Field(default=None, min_length=1, max_length=255)
    error_policy: Literal["allow", "block"] = "block"
    unresolved_policy: Literal["allow", "block"] = "block"
    evidence: list[OperationEvidence] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_proposal(self) -> Self:
        if (self.target_type is None) != (self.target_id is None):
            raise ValueError("target_type and target_id must be supplied together")
        if self.step.type != "tool":
            raise ValueError("Operation records require a tool step")
        if self.step.output is not None:
            raise ValueError("Operation proposals cannot include a tool output")
        if self.step.ground_truth is not None:
            raise ValueError("Operation proposals cannot include ground truth")
        if self.step.context is not None and "norma_operation_evidence" in self.step.context:
            raise ValueError("step.context.norma_operation_evidence is reserved")
        try:
            serialized = json.dumps(self.model_dump(mode="json"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Operation proposal must contain finite JSON values") from exc
        if len(serialized.encode()) > 65536:
            raise ValueError("Operation proposal exceeds the 65536-byte budget")
        return self

    @field_validator("agent_name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))


class CreateOperationRequest(OperationProposal):
    """A hosted-only proposal; failed or unresolved checks block new operations."""

    execution_mode: Literal["hosted"] = "hosted"
    error_policy: Literal["block"] = "block"
    unresolved_policy: Literal["block"] = "block"


class OperationRuleSnapshot(BaseModel):
    """Exact server-owned rule definition and live check disposition."""

    rule_id: int
    name: str
    version: int | None = None
    definition: RuleDefinitionRuntime
    definition_fingerprint: str
    disposition: Literal["evaluated", "cancelled", "skipped", "unknown"]
    agent_generation: str | None = None
    control_resolution: ControlReceipt | None = None


class OperationDecision(BaseModel):
    """The policy result from actual server-side rule check."""

    outcome: Literal["allow", "deny", "steer"]
    reason: str
    check: CheckResponse
    # Accept old stored records, but deployment/provider mappings are never public API evidence.


class OperationObservation(OperationRequestModel):
    """Append-only caller report about what happened after the decision."""

    id: str
    event_key: str = Field(min_length=1, max_length=128)
    kind: Literal["attempted", "succeeded", "failed", "unknown"]
    summary: str = Field(min_length=1, max_length=2000)
    external_reference: str | None = Field(default=None, max_length=255)
    recorded_at: datetime
    source: Literal["caller_reported"] = "caller_reported"


class OperationVerification(BaseModel):
    """Immutable snapshot of a linked, trusted postcondition assessment."""

    session_id: str
    revision: int
    claim: Literal["postcondition_assessed"] = "postcondition_assessed"
    assessment: VerificationAssessment
    linked_at: datetime


class OperationRecord(BaseModel):
    """The frozen decision, its rule snapshot, and independent effect evidence."""

    id: str
    operation_key: str
    agent_name: str
    agent_generation: str | None = None
    control_contract: Literal["rule-controls-v1"] | None = None
    target_type: str | None = None
    target_id: str | None = None
    created_at: datetime
    request_fingerprint: str
    request: OperationProposal
    decision: OperationDecision
    rules: list[OperationRuleSnapshot] = Field(default_factory=list)
    observations: list[OperationObservation] = Field(default_factory=list)
    verifications: list[OperationVerification] = Field(default_factory=list)
    discrepancy: Literal["none", "reported_execution_after_denial"] = "none"
    current_outcome: Literal[
        "not_observed", "attempted", "succeeded", "failed", "unknown", "conflicting"
    ] = "not_observed"


class OperationListResponse(BaseModel):
    """Operations for one authorized agent."""

    operations: list[OperationRecord] = Field(default_factory=list)
    control_contract: Literal["rule-controls-v1"] | None = None


class AddOperationObservationRequest(OperationRequestModel):
    """Append one caller-reported effect event with an idempotency key."""

    agent_name: str = Field(
        min_length=AGENT_NAME_MIN_LENGTH,
        max_length=255,
        pattern=AGENT_NAME_PATTERN,
    )
    event_key: str = Field(min_length=1, max_length=128)
    kind: Literal["attempted", "succeeded", "failed", "unknown"]
    summary: str = Field(min_length=1, max_length=2000)
    external_reference: str | None = Field(default=None, max_length=255)
    target_type: str | None = Field(default=None, min_length=1, max_length=255)
    target_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def paired_target(self) -> Self:
        if (self.target_type is None) != (self.target_id is None):
            raise ValueError("target_type and target_id must be supplied together")
        return self

    @field_validator("agent_name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))


class LinkOperationVerificationRequest(OperationRequestModel):
    """Link an existing verification session to its exact operation subject."""

    agent_name: str = Field(
        min_length=AGENT_NAME_MIN_LENGTH,
        max_length=255,
        pattern=AGENT_NAME_PATTERN,
    )
    verification_session_id: str = Field(min_length=1, max_length=128)
    target_type: str | None = Field(default=None, min_length=1, max_length=255)
    target_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def paired_target(self) -> Self:
        if (self.target_type is None) != (self.target_id is None):
            raise ValueError("target_type and target_id must be supplied together")
        return self

    @field_validator("agent_name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))
