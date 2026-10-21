# Modified for Norma.
"""Hosted observation wire contracts; no policy authoring or inference."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import ConfigDict, Field

from ..models.agent import JSONValue
from ..models.base import BaseModel

type CheckOutcome = Literal["matched", "not_matched", "unresolved"]
type EvidenceOrigin = Literal["caller_asserted", "runtime_observed", "source_verified"]
type EvidenceTrust = EvidenceOrigin
type EvidenceStatus = Literal["present", "missing", "stale", "conflicting", "truncated", "redacted"]
type Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")]
type Instructions = Annotated[str, Field(min_length=1, max_length=16000)]


class SemanticModel(BaseModel):
    """Reject misspelled semantic configuration rather than silently ignoring it."""

    model_config = ConfigDict(extra="forbid")


class EvidenceItem(SemanticModel):
    """An assembled, fingerprinted evidence value and its actual provenance."""

    name: str
    path: str
    value: JSONValue = None
    required: bool = True
    origin: EvidenceOrigin = "caller_asserted"
    status: EvidenceStatus = "present"
    source_id: str | None = None
    source_version: str | None = None
    observed_at: datetime | None = None
    fingerprint: str
    truncated: bool = False


class EvidenceBundle(SemanticModel):
    """The bounded evidence used by a semantic observation."""

    items: list[EvidenceItem] = Field(default_factory=list)
    fingerprint: str
    complete: bool
    missing_required: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class SemanticStatementAnswer(SemanticModel):
    """A graded truth value, not a confidence estimate."""

    type: Literal["statement"] = "statement"
    support_score: Probability


class SemanticChoiceAnswer(SemanticModel):
    """A selected candidate with its full probability distribution."""

    type: Literal["choice"] = "choice"
    choice: str
    confidence: Probability
    probabilities: dict[str, Probability]


class SemanticScoreAnswer(SemanticModel):
    """An ordered-rubric score retaining distribution and legend."""

    type: Literal["score"] = "score"
    score: float = Field(ge=0, allow_inf_nan=False)
    confidence: Probability
    legend: dict[str, str]
    probabilities: dict[str, Probability]


type SemanticAnswer = Annotated[
    SemanticStatementAnswer | SemanticChoiceAnswer | SemanticScoreAnswer,
    Field(discriminator="type"),
]
StatementAnswer = SemanticStatementAnswer
ChoiceAnswer = SemanticChoiceAnswer
ScoreAnswer = SemanticScoreAnswer


class SemanticUsage(SemanticModel):
    """Usage reported for a bounded inference operation."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class SemanticObservation(SemanticModel):
    """Public evidence and answers; provider identity and secrets are deliberately absent."""

    observation_id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["completed", "insufficient_evidence", "ambiguous", "provider_unavailable"]
    answers: dict[str, SemanticAnswer] = Field(default_factory=dict)
    rule_outcomes: dict[str, CheckOutcome] = Field(
        default_factory=dict,
        exclude_if=lambda value: not value,
        description="Current deterministic decision-rule outcomes, recalculated on policy replay",
    )
    evidence: EvidenceBundle
    definition_fingerprint: str
    question_fingerprint: str
    evidence_fingerprint: str
    check_release: str
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    usage: SemanticUsage | None = None
