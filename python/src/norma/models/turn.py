# Modified for Norma.
"""Bounded, redacted contracts for evaluating an agent's finish."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from .base import BaseModel
from .control_evidence import ControlEvidenceSnapshot, ControlReceipt

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
# A clipped evidence prefix can end immediately before "Bearer"; requiring a
# leading word boundary would expose the credential in that otherwise valid turn.
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+={0,2}", re.IGNORECASE)
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_GCP_KEY = re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")
_STRIPE_KEY = re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b")
_ASSIGNMENT = re.compile(
    r"(?P<prefix>\b[A-Za-z_][A-Za-z0-9_]*_(?:TOKEN|SECRET|PASSWORD|KEY)\b"
    r"[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)",
    re.IGNORECASE,
)
_REASON_CODES = frozenset(
    {
        "claim_contradicted",
        "verification_claim_unsupported",
        "failure_not_reported",
        "request_not_addressed",
    }
)
_CUSTOM_REASON_CODE = re.compile(r"custom:[a-z0-9]+(?:-[a-z0-9]+)*$")


def redact_turn_text(text: str) -> str:
    """Replace common credential forms deterministically before storage or inference."""
    redacted = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _AWS_KEY.sub("[REDACTED AWS KEY]", redacted)
    redacted = _GCP_KEY.sub("[REDACTED GCP KEY]", redacted)
    redacted = _STRIPE_KEY.sub("[REDACTED STRIPE KEY]", redacted)
    return _ASSIGNMENT.sub(lambda match: match.group("prefix") + "[REDACTED]", redacted)


class _StrictTurnModel(BaseModel):
    """Forbid unknown fields and coercion in the externally supplied turn payload."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ActionSummary(_StrictTurnModel):
    """One normalized action and a bounded excerpt of its result."""

    index: int = Field(ge=0)
    kind: Literal["shell", "edit", "read", "search", "web", "mcp", "task", "other"]
    native_tool: str = Field(max_length=256)
    target: str | None = Field(default=None, max_length=300)
    command: str | None = Field(default=None, max_length=500)
    status: Literal["succeeded", "failed", "denied", "unknown"]
    exit_code: int | None = None
    output_tail: str | None = Field(default=None, max_length=1000)
    duration_ms: int | None = Field(default=None, ge=0)

    @field_validator("native_tool", "target", "command", "output_tail", mode="before")
    @classmethod
    def redact_text(cls, value: object) -> object:
        return redact_turn_text(value) if isinstance(value, str) else value


class ContinuationState(_StrictTurnModel):
    """Host-maintained count and requested limit for finish retries.

    The schema accepts three for older installed clients; the launch evaluator
    clamps the effective limit to two.
    """

    count: int = Field(ge=0)
    max: int = Field(default=2, ge=0, le=3)


class TurnRecord(_StrictTurnModel):
    """The bounded evidence sent when an agent proposes to finish a turn."""

    schema_version: Literal["1"]
    platform: str = Field(min_length=1, max_length=256)
    platform_version: str | None = Field(default=None, max_length=256)
    model: str | None = Field(default=None, max_length=256)
    session_id: str = Field(min_length=1, max_length=512)
    turn_id: str = Field(min_length=1, max_length=512)
    user_request: str | None = Field(default=None, max_length=2000)
    final_message: str = Field(max_length=8000)
    actions: list[ActionSummary] = Field(default_factory=list, max_length=50)
    actions_truncated: bool = False
    files_touched: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=200
    )
    evidence_source: Literal["native_hooks", "sdk", "self_report", "telemetry"]
    continuation: ContinuationState

    @model_validator(mode="before")
    @classmethod
    def keep_recent_actions(cls, value: object) -> object:
        if isinstance(value, dict) and isinstance(value.get("actions"), list):
            actions = value["actions"]
            if len(actions) > 50:
                normalized = dict(value)
                normalized["actions"] = actions[-50:]
                normalized["actions_truncated"] = True
                return normalized
        return value

    @field_validator(
        "platform",
        "platform_version",
        "model",
        "session_id",
        "turn_id",
        "user_request",
        "final_message",
        mode="before",
    )
    @classmethod
    def redact_text(cls, value: object) -> object:
        return redact_turn_text(value) if isinstance(value, str) else value

    @field_validator("files_touched", mode="before")
    @classmethod
    def redact_paths(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [redact_turn_text(path) if isinstance(path, str) else path for path in value]


def redact_turn_record(record: TurnRecord) -> TurnRecord:
    """Revalidate and redact a turn before persistence or provider calls."""
    return TurnRecord.model_validate(record.model_dump(mode="json"))


class FinishFinding(_StrictTurnModel):
    """A resolved, redacted conflict between one claim and one evidence line."""

    claim_id: str = Field(min_length=1, max_length=80)
    reason_code: str = Field(min_length=1, max_length=100)
    claim: str = Field(max_length=160)
    evidence: str = Field(max_length=300)

    @field_validator("claim_id", "reason_code", "claim", "evidence", mode="before")
    @classmethod
    def redact_text(cls, value: object) -> object:
        return redact_turn_text(value) if isinstance(value, str) else value

    @field_validator("reason_code")
    @classmethod
    def check_reason_code(cls, value: str) -> str:
        if value not in _REASON_CODES and not _CUSTOM_REASON_CODE.fullmatch(value):
            raise ValueError("reason_code must be a stable finish reason code")
        return value


class FinishVerdict(_StrictTurnModel):
    """Canonical finish decision; outcome records a terminal unverified finish separately."""

    execution_contract: str | None = None
    control_contract: Literal["rule-controls-v1"] | None = None
    agent_generation: str | None = Field(default=None, max_length=128)
    control_resolutions: list[ControlReceipt] | None = None
    control_evidence: list[ControlEvidenceSnapshot] | None = None
    verdict: Literal["pass", "hold", "unsure", "unavailable"]
    reason: str = Field(max_length=600)
    reason_codes: list[str] = Field(default_factory=list)
    held_by: list[int] = Field(default_factory=list)
    would_hold_by: list[int] = Field(default_factory=list)
    continuation: ContinuationState
    allow_finish: bool
    outcome: Literal["completed", "held", "finished_unverified"]
    provider_input_tokens: int | None = Field(default=None, ge=0)

    @field_validator("reason", mode="before")
    @classmethod
    def redact_reason(cls, value: object) -> object:
        return redact_turn_text(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def check_finish_behavior(self) -> Self:
        if self.verdict == "hold":
            expected_allow = False
            expected_outcome = "held"
            if self.continuation.count >= self.continuation.max:
                raise ValueError("a finish at the loop guard cannot have verdict hold")
        else:
            expected_allow = True
            expected_outcome = "completed" if self.verdict == "pass" else "finished_unverified"
        if self.allow_finish != expected_allow or self.outcome != expected_outcome:
            raise ValueError("allow_finish and outcome must agree with verdict and loop guard")
        return self
