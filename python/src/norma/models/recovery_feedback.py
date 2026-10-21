# Modified for Norma.
"""Server-issued recovery feedback. The SDK does not derive judgments."""

from typing import Literal

from pydantic import Field

from .semantic import EvidenceOrigin, EvidenceStatus, SemanticModel

type RecoveryReason = Literal[
    "criterion_triggered",
    "missing_evidence",
    "stale_evidence",
    "uncertain_judgment",
    "insufficient_origin",
    "truncated_evidence",
    "check_error",
]
type _ConditionOutcome = Literal["matched", "not_matched", "unresolved", "error"]


class RecoveryEvidenceReference(SemanticModel):
    """Identify the observed evidence without copying its value or asserting trust."""

    binding: str
    path: str
    required: bool = True
    unresolved: bool = False
    status: EvidenceStatus
    origin: EvidenceOrigin
    source_id: str | None = None
    source_version: str | None = None
    fingerprint: str


class RecoveryFeedback(SemanticModel):
    """A deterministic explanation of a failed or unresolved Rule check."""

    rule_id: int
    check_id: str
    outcome: Literal["matched", "unresolved", "error"]
    reason: RecoveryReason
    condition_ids: list[str] = Field(default_factory=list)
    condition_path: str | None = None
    condition_outcome: _ConditionOutcome | None = None
    negated: bool = False
    observation_id: str | None = None
    definition_fingerprint: str | None = None
    evidence_fingerprint: str | None = None
    evidence: list[RecoveryEvidenceReference] = Field(default_factory=list)
