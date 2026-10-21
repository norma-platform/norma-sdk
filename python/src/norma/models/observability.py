# Modified for Norma.
"""Observability models for tracking rule executions.

This module provides models for:
- Rule execution events (what rules ran, with what results)
- Batch event ingestion (SDK -> Server)
- Event queries (filtering and pagination)
- Aggregated statistics (pre-computed metrics)
- Real-time streaming updates
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator

from .actions import (
    ActionDecision,
    normalize_action,
)
from .agent import AGENT_NAME_MIN_LENGTH, AGENT_NAME_PATTERN, normalize_agent_name
from .base import BaseModel

# =============================================================================
# Core Event Model
# =============================================================================


class RuleCheckEvent(BaseModel):
    """
    Represents one observed rule result, not a distinct request or proof an action ran.

    Trace/span IDs are producer-reported correlation values that can repeat across
    checks. Execution identity is scoped to the account, rule, and check stage.

    This is the core observability data model, capturing:
    - Identity: check_id; correlation: trace_id and span_id
    - Context: agent, rule, check stage, applies to
    - Result: recorded rule response, whether matched, evaluator confidence score
    - Timing: when it happened, how long it took
    - Optional details: evaluator name, selector path, errors, metadata

    Attributes:
        check_id: Unique ID for this specific rule execution
        trace_id: Producer-reported trace correlation ID; may repeat across checks
        span_id: Producer-reported span correlation ID; may repeat across checks
        agent_name: Identifier of the agent that executed the rule
        rule_id: Database ID of the rule
        rule_name: Name of the rule (denormalized for queries)
        check_stage: "pre" (before execution) or "post" (after execution)
        applies_to: "llm_call", "tool_call", or a custom step type with a "_call" suffix
        action: Recorded configured/effective rule response (deny, steer, observe)
        matched: Whether the rule evaluator matched
        confidence: Confidence score from the evaluator (0.0-1.0)
        timestamp: When the rule was executed (UTC)
        execution_duration_ms: How long the rule check took
        evaluator_name: Name of the evaluator used
        selector_path: The selector path used to extract data
        error_message: Error message if check failed
        metadata: Additional metadata for extensibility
    """

    # Execution identity and producer-reported correlation values
    check_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=1,
        max_length=36,
        description="Unique ID for this rule execution",
    )
    trace_id: str = Field(
        ...,
        min_length=1,
        description="Producer-reported trace correlation ID; may repeat across checks",
    )
    span_id: str = Field(
        ...,
        min_length=1,
        description="Producer-reported span correlation ID; may repeat across checks",
    )

    # Agent identity
    agent_name: str = Field(
        ...,
        min_length=AGENT_NAME_MIN_LENGTH,
        pattern=AGENT_NAME_PATTERN,
        description="Identifier of the agent",
    )

    # Rule info
    rule_id: int = Field(..., description="Database ID of the rule")
    rule_name: str = Field(..., description="Name of the rule (denormalized)")

    # Execution context
    check_stage: Literal["pre", "post"] = Field(..., description="Check stage: 'pre' or 'post'")
    applies_to: str = Field(
        ...,
        min_length=1,
        description="Type of call or custom step type with a '_call' suffix",
    )

    # Result
    action: ActionDecision = Field(
        ...,
        description="Recorded configured/effective rule response, not an external action receipt",
    )
    matched: bool = Field(..., description="Whether the evaluator matched (True) or not (False)")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score (0.0 to 1.0)")

    # Timing
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the rule was executed (UTC)",
    )
    execution_duration_ms: float | None = Field(
        default=None, ge=0, description="Execution duration in milliseconds"
    )

    # Optional details
    evaluator_name: str | None = Field(default=None, description="Name of the evaluator used")
    selector_path: str | None = Field(
        default=None, description="Selector path used to extract data"
    )
    error_message: str | None = Field(default=None, description="Error message if check failed")

    # Extensibility
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    def _to_wire(self) -> dict[str, Any]:
        """Serialize an event for the private runtime ingestion protocol."""
        return self.model_dump(mode="json")

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, v: str) -> str:
        """
        Validate trace_id is non-empty.

        We use lenient validation to support different tracing backends
        (OTEL, X-Ray, Jaeger, etc.) and user-provided trace IDs.
        The SDK generates OTEL-compatible IDs (32-char hex), but we accept
        any non-empty string for flexibility.
        """
        if not v or not v.strip():
            raise ValueError("trace_id cannot be empty")
        return v

    @field_validator("span_id")
    @classmethod
    def validate_span_id(cls, v: str) -> str:
        """
        Validate span_id is non-empty.

        We use lenient validation to support different tracing backends
        (OTEL, X-Ray, Jaeger, etc.) and user-provided span IDs.
        The SDK generates OTEL-compatible IDs (16-char hex), but we accept
        any non-empty string for flexibility.
        """
        if not v or not v.strip():
            raise ValueError("span_id cannot be empty")
        return v

    @field_validator("agent_name", mode="before")
    @classmethod
    def validate_and_normalize_agent_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))

    @field_validator("action", mode="before")
    @classmethod
    def normalize_event_action(cls, value: str) -> ActionDecision:
        return normalize_action(value)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "check_id": "550e8400-e29b-41d4-a716-446655440000",
                    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                    "span_id": "00f067aa0ba902b7",
                    "agent_name": "my-agent",
                    "rule_id": 123,
                    "rule_name": "sql-injection-check",
                    "check_stage": "pre",
                    "applies_to": "llm_call",
                    "action": "deny",
                    "matched": True,
                    "confidence": 0.95,
                    "timestamp": "2025-01-09T10:30:00Z",
                    "execution_duration_ms": 15.3,
                    "evaluator_name": "regex",
                    "selector_path": "input",
                }
            ]
        }
    }


# =============================================================================
# Batch Ingestion Models
# =============================================================================


class BatchEventsRequest(BaseModel):
    """
    Request model for batch event ingestion.

    SDKs batch events and send them to the server periodically.
    This reduces HTTP overhead significantly (100x reduction).

    Attributes:
        events: List of rule execution events to ingest
    """

    events: list[RuleCheckEvent] = Field(
        ..., min_length=1, max_length=1000, description="List of events to ingest"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "events": [
                        {
                            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                            "span_id": "00f067aa0ba902b7",
                            "agent_name": "my-agent",
                            "rule_id": 123,
                            "rule_name": "sql-injection-check",
                            "check_stage": "pre",
                            "applies_to": "llm_call",
                            "action": "deny",
                            "matched": True,
                            "confidence": 0.95,
                        }
                    ]
                }
            ]
        }
    }


class BatchEventsResponse(BaseModel):
    """
    Response model for batch event ingestion.

    Attributes:
        received: Number of events received
        enqueued: Number of events acknowledged, including successful idempotent replays
        dropped: Number of events dropped (queue full or delivery failed)
        duplicates: Number of acknowledged replays when known
        stored: Number of newly stored observations when the store supplies a receipt
        status: Overall status ('queued', 'partial', 'failed')
    """

    received: int = Field(..., ge=0, description="Number of events received")
    enqueued: int = Field(
        ...,
        ge=0,
        description="Acknowledged events, including idempotent replays",
    )
    dropped: int = Field(..., ge=0, description="Number of events dropped")
    duplicates: int = Field(default=0, ge=0, description="Acknowledged idempotent replays")
    stored: int | None = Field(default=None, ge=0, description="Newly stored observations if known")
    status: Literal["queued", "partial", "failed"] = Field(
        ..., description="Overall ingestion status"
    )


# =============================================================================
# Query Models
# =============================================================================
