# Modified for Norma.
"""Postcondition assessment receipts returned by the hosted API."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .semantic import EvidenceOrigin, SemanticAnswer, SemanticModel, SemanticUsage

type VerificationOutcome = Literal[
    "success", "failed", "partial", "stale", "unknown", "conflicting"
]
type VerificationCriterionStatus = Literal[
    "satisfied", "unsatisfied", "stale", "unknown", "conflicting"
]


class VerificationEvidenceReference(SemanticModel):
    """Actual evidence provenance and citation; raw source values are not retained here."""

    id: str
    evidence_id: str
    origin: EvidenceOrigin
    source_id: str | None = None
    source_version: str | None = None
    subject_id: str
    subject_version: str
    correlation_id: str | None = None
    related_subject_ids: list[str] = Field(default_factory=list)
    observed_at: datetime | None = None
    received_at: datetime
    fingerprint: str
    status: Literal["available", "missing", "stale", "untrusted", "conflicting", "unavailable"]


class VerificationCriterionResult(SemanticModel):
    criterion_id: str
    status: VerificationCriterionStatus
    basis: Literal["exact_check", "semantic_inference"]
    reason: str
    evidence_ids: list[str]
    observed_value_fingerprint: str | None = None


class VerificationJudgment(SemanticModel):
    """Inferred answers remain distinct from observed source facts."""

    check_release: str
    question_fingerprint: str
    answers: dict[str, SemanticAnswer]
    rule_outcomes: dict[str, Literal["matched", "not_matched", "unresolved"]]
    usage: SemanticUsage | None = None
    context_evidence_ids: list[str] = Field(default_factory=list)


class VerificationAssessment(SemanticModel):
    revision: int
    subject_version: str
    definition_fingerprint: str
    previous_assessment_fingerprint: str | None = None
    fingerprint: str
    outcome: VerificationOutcome
    claim_scope: Literal[
        "submitted_artifact", "observed_state", "correlated_external_effect", "related_sequence"
    ]
    criteria: list[VerificationCriterionResult]
    observed_facts: list[VerificationEvidenceReference]
    inferred_judgment: VerificationJudgment | None = None
    satisfied_criteria: list[str]
    unsatisfied_criteria: list[str]
    assessed_at: datetime
    valid_until: datetime | None = None
    note: str | None = None
