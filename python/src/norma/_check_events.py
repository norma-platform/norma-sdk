# Modified for Norma.
"""Derived Rule-execution event reconstruction for SDK check flows."""

from collections.abc import Mapping
from datetime import UTC, datetime

from norma._wire.exports import (
    CheckRequest,
    CheckResponse,
    RuleCheckEvent,
    RuleDefinition,
    RuleDefinitionRuntime,
    RuleMatch,
)

from .observability import get_logger, is_observability_enabled, write_events

_logger = get_logger(__name__)

# All-zero values are invalid trace/span IDs per OpenTelemetry and make it
# obvious that the event could not be correlated to an external trace.
_FALLBACK_TRACE_ID = "0" * 32
_FALLBACK_SPAN_ID = "0" * 16
_trace_warning_logged = False
_DEBUG_METADATA_KEYS = frozenset(
    {
        "selected_data",
        "selected_data_preview",
        "engine_selected_data",
        "engine_selected_data_preview",
    }
)


def _safe_event_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Drop raw/debug metadata that should not be exported as observability data."""
    safe_metadata = {
        key: _safe_trace_value(value)
        for key, value in metadata.items()
        if key not in _DEBUG_METADATA_KEYS
    }
    if "input" not in safe_metadata:
        for preview_key in ("engine_selected_data_preview", "selected_data_preview"):
            preview = metadata.get(preview_key)
            if isinstance(preview, dict) and "value" in preview:
                safe_metadata["input"] = _safe_trace_value(preview["value"])
                break
    return safe_metadata


def _safe_trace_value(value: object) -> object:
    """Strip selected evidence from every nested semantic condition trace."""
    if isinstance(value, list):
        return [_safe_trace_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    safe: dict[str, object] = {}
    for key, item in value.items():
        if key in _DEBUG_METADATA_KEYS:
            continue
        if key == "semantic_observation":
            if isinstance(item, dict):
                safe[key] = {
                    name: item[name]
                    for name in (
                        "observation_id",
                        "check_release",
                        "status",
                    )
                    if name in item
                }
        else:
            safe[key] = _safe_trace_value(item)
    return safe


def map_applies_to(step_type: str) -> str:
    """Map Norma step types to observability applies_to values."""
    if step_type == "tool":
        return "tool_call"
    if step_type == "llm":
        return "llm_call"
    return f"{step_type}_call"


def _resolve_event_trace_context(
    trace_id: str | None,
    span_id: str | None,
) -> tuple[str, str]:
    """Return event IDs, applying fallback IDs and a one-time warning if needed."""
    global _trace_warning_logged  # noqa: PLW0603

    if trace_id and span_id:
        return trace_id, span_id

    if not _trace_warning_logged:
        _logger.warning(
            "Emitting Rule events without trace context; events will use fallback "
            "IDs and cannot be correlated with traces. Pass trace_id/span_id for "
            "full observability."
        )
        _trace_warning_logged = True

    return trace_id or _FALLBACK_TRACE_ID, span_id or _FALLBACK_SPAN_ID


def _build_events_for_matches(
    matches: list[RuleMatch] | None,
    *,
    matched: bool,
    include_error_message: bool,
    request: CheckRequest,
    rule_lookup: Mapping[int, RuleDefinition | RuleDefinitionRuntime],
    trace_id: str,
    span_id: str,
    agent_name: str,
    now: datetime,
) -> list[RuleCheckEvent]:
    if not matches:
        return []

    applies_to = map_applies_to(request.step.type)
    events: list[RuleCheckEvent] = []

    for match in matches:
        if not match.check_id:
            # Missing server identity cannot be promoted to an observed check receipt.
            continue
        event_metadata = _safe_event_metadata(dict(match.result.metadata or {}))
        event_metadata["execution_id_source"] = (
            "engine" if "check_id" in match.model_fields_set else "sdk_occurrence"
        )
        event_metadata["rule_mode"] = match.mode
        event_metadata["rule_mode_contract"] = "rule-mode-v1"
        event_metadata["configured_action"] = match.action
        if match.result.message is not None:
            event_metadata["reason"] = match.result.message
        if match.result.outcome is not None:
            event_metadata["outcome"] = match.result.outcome
            event_metadata["configured_action"] = match.action
            event_metadata["failed_condition_ids"] = match.result.failed_condition_ids
            event_metadata["unresolved_evidence_ids"] = match.result.unresolved_evidence_ids
        if match.result.semantic_observation is not None:
            # The durable server ledger owns evidence; events only reference that observation.
            observation = match.result.semantic_observation
            event_metadata["semantic_observation_id"] = observation.observation_id
            event_metadata["check_release"] = observation.check_release
        selector_path = None
        evaluator_name = None

        events.append(
            RuleCheckEvent(
                check_id=match.check_id,
                trace_id=trace_id,
                span_id=span_id,
                agent_name=agent_name,
                rule_id=match.rule_id,
                rule_name=match.rule_name,
                check_stage=request.stage,
                applies_to=applies_to,
                action=match.effective_action or match.action,
                matched=matched,
                confidence=match.result.confidence,
                timestamp=now,
                evaluator_name=evaluator_name,
                selector_path=selector_path,
                error_message=match.result.error if include_error_message else None,
                metadata=event_metadata,
            )
        )

    return events


def build_rule_check_events(
    response: CheckResponse,
    request: CheckRequest,
    rule_lookup: Mapping[int, RuleDefinition | RuleDefinitionRuntime],
    trace_id: str | None,
    span_id: str | None,
    agent_name: str | None,
) -> list[RuleCheckEvent]:
    """Reconstruct Rule execution events from an check response.

    This is the shared reconstruction step used by both supported event
    creation styles:
    - the default SDK observability path, where reconstructed local events are
      queued into the existing SDK batcher
    - the merged-event path, where local and server events are reconstructed in
      the SDK and queued together through the existing SDK batcher

    Args:
        response: Check response containing matches, errors, and
            non-matches.
        request: Original check request used to derive stage and
            ``applies_to``.
        rule_lookup: Parsed Rules keyed by Rule ID.
        trace_id: Optional trace ID for correlation.
        span_id: Optional span ID for correlation.
        agent_name: Optional override for the agent name stamped on events.

    Returns:
        A list of reconstructed ``RuleCheckEvent`` objects.
    """
    resolved_trace_id, resolved_span_id = _resolve_event_trace_context(trace_id, span_id)
    resolved_agent_name = agent_name or request.agent_name
    now = datetime.now(UTC)

    events: list[RuleCheckEvent] = []
    events.extend(
        _build_events_for_matches(
            response.matches,
            matched=True,
            include_error_message=True,
            request=request,
            rule_lookup=rule_lookup,
            trace_id=resolved_trace_id,
            span_id=resolved_span_id,
            agent_name=resolved_agent_name,
            now=now,
        )
    )
    events.extend(
        _build_events_for_matches(
            response.errors,
            matched=False,
            include_error_message=True,
            request=request,
            rule_lookup=rule_lookup,
            trace_id=resolved_trace_id,
            span_id=resolved_span_id,
            agent_name=resolved_agent_name,
            now=now,
        )
    )
    events.extend(
        _build_events_for_matches(
            response.non_matches,
            matched=False,
            include_error_message=False,
            request=request,
            rule_lookup=rule_lookup,
            trace_id=resolved_trace_id,
            span_id=resolved_span_id,
            agent_name=resolved_agent_name,
            now=now,
        )
    )
    events.extend(
        _build_events_for_matches(
            response.unresolved,
            matched=False,
            include_error_message=False,
            request=request,
            rule_lookup=rule_lookup,
            trace_id=resolved_trace_id,
            span_id=resolved_span_id,
            agent_name=resolved_agent_name,
            now=now,
        )
    )
    return events


def enqueue_observability_events(events: list[RuleCheckEvent]) -> None:
    """Enqueue reconstructed events through the existing SDK observability path.

    This preserves the built-in SDK behavior of forwarding events through the
    existing observability batcher.

    Args:
        events: Reconstructed Rule execution events to enqueue.

    Returns:
        None.
    """
    if not is_observability_enabled():
        return

    result = write_events(events)
    if result.dropped:
        _logger.warning(
            "Norma could not queue %d Rule event(s). Check the event sink and flush receipt.",
            result.dropped,
        )
