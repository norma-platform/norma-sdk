# Modified for Norma.
"""Remote-only hosted check; uncertainty never authorizes user execution."""

from collections.abc import Mapping
from typing import Any, Literal

import httpx

from ._check_events import build_rule_check_events, enqueue_observability_events
from ._state import state
from ._wire.exports import (
    CheckRequest,
    CheckResponse,
    CheckResult,
    JSONValue,
    RuleCheckEvent,
    RuleDefinition,
    RuleDefinitionRuntime,
    Step,
)
from .client import NormaClient
from .errors import raise_for_norma_status
from .observability import get_logger, is_observability_enabled
from .tracing import get_trace_and_span_ids
from .validation import ensure_agent_name, ensure_step_type

_logger = get_logger(__name__)


def _validate_target_pair(target_type: str | None, target_id: str | None) -> None:
    """Reject half-supplied target pairs; both or neither must be present."""
    if (target_type is None) != (target_id is None):
        raise ValueError("target_type and target_id must be supplied together.")


def _resolve_session_target(
    target_type: str | None, target_id: str | None
) -> tuple[str | None, str | None]:
    """Resolve the session target and reject accidental cross-target check."""
    if target_type is None and target_id is None:
        return state.target_type, state.target_id
    _validate_target_pair(target_type, target_id)
    if state.current_agent is not None and (
        target_type != state.target_type or target_id != state.target_id
    ):
        raise ValueError(
            "Per-call target context must match the target context fixed at "
            "init() time. The SDK supports one target per session "
            "(including no-target sessions); re-init to change it."
        )
    return target_type, target_id


async def _post_checks_request(
    client: NormaClient,
    *,
    request_payload: dict[str, Any],
    headers: dict[str, str] | None,
    target_type: str | None,
    target_id: str | None,
) -> httpx.Response:
    response = await client.post_runtime_checks(
        json=request_payload, headers=headers, target_type=target_type, target_id=target_id
    )

    raise_for_norma_status(response)
    data = response.json()
    if not isinstance(data, dict) or data.get("execution_contract") != "hosted-v1":
        raise RuntimeError("Unsupported hosted runtime: execution_contract hosted-v1 is required.")
    return response


def _parse_checks_response(response: httpx.Response) -> CheckResponse:
    data = response.json()
    if not isinstance(data, dict) or type(data.get("allowed")) is not bool:
        raise RuntimeError("Norma did not return a valid decision")
    if data.get("execution_contract") != "hosted-v1":
        raise RuntimeError("Unsupported hosted runtime: execution_contract hosted-v1 is required.")
    return CheckResponse.model_validate(data)


async def request_remote_check(
    client: NormaClient,
    agent_name: str,
    step: Step,
    stage: Literal["pre", "post"],
    rules: list[dict[str, Any]],
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    event_agent_name: str | None = None,
) -> CheckResult:
    """Reject local Rules, then always ask the hosted runtime, including empty caches."""
    for rule in rules:
        definition = RuleDefinition.model_validate(rule.get("rule"))
        if definition.execution == "sdk":
            raise ValueError("SDK-local Rules are unsupported; migrate to hosted execution.")
    return await request_check(
        client,
        agent_name,
        step,
        stage,
        target_type=target_type,
        target_id=target_id,
        trace_id=trace_id,
        span_id=span_id,
    )


def _as_checks_result(response: CheckResponse) -> CheckResult:
    """Keep nested identity presence and normalized feedback during conversion."""
    result = CheckResult.model_validate(response, from_attributes=True)
    return result.model_copy(update={"recovery_feedback": response.recovery_feedback})


def _observability_enabled() -> bool:
    """Reporting configuration failures must not alter a safety decision."""
    try:
        return is_observability_enabled()
    except Exception:
        _logger.warning("Rule event reporting is unavailable")
        return False


def _build_events_safely(
    response: CheckResponse,
    request: CheckRequest,
    rule_lookup: Mapping[int, RuleDefinition | RuleDefinitionRuntime],
    trace_id: str | None,
    span_id: str | None,
    agent_name: str | None,
) -> list[RuleCheckEvent]:
    """Reconstruct observations without allowing reporting to change enforcement."""
    try:
        return build_rule_check_events(
            response,
            request,
            rule_lookup,
            trace_id,
            span_id,
            agent_name,
        )
    except Exception:
        # Do not log payloads or exception text: reporting may contain private evidence.
        _logger.warning("Rule event reporting could not build observations")
        return []


def _enqueue_events_safely(events: list[RuleCheckEvent]) -> None:
    """Isolate sink failures, including while preserving an check exception."""
    try:
        enqueue_observability_events(events)
    except Exception:
        _logger.warning("Rule event reporting could not enqueue observations")


async def request_check(
    client: NormaClient,
    agent_name: str,
    step: Step,
    stage: Literal["pre", "post"],
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> CheckResult:
    """Check if agent interaction is safe through the public SDK helper.

    The server returns only check semantics. When SDK observability is
    enabled, this helper reconstructs server-side Rule-execution events
    from the response and enqueues them through the built-in SDK batcher.

    When ``target_type`` and ``target_id`` are both supplied, the request
    is target-bearing and the server merges target bindings into the
    effective Rule set. Both or neither must be provided; otherwise
    the helper raises ``ValueError``. The caller owns the supplied
    ``client`` and is responsible for any session-target consistency
    rules at higher layers.
    """
    _validate_target_pair(target_type, target_id)

    normalized_name = ensure_agent_name(agent_name)
    default_trace, default_span = get_trace_and_span_ids()
    resolved_trace_id, resolved_span_id = trace_id or default_trace, span_id or default_span
    request = CheckRequest(
        agent_name=normalized_name,
        step=step,
        stage=stage,
        target_type=target_type,
        target_id=target_id,
    )
    request_payload = request.model_dump(mode="json")

    response = await _post_checks_request(
        client,
        request_payload=request_payload,
        headers={
            key: value
            for key, value in {
                "X-Trace-Id": resolved_trace_id,
                "X-Span-Id": resolved_span_id,
            }.items()
            if value
        },
        target_type=target_type,
        target_id=target_id,
    )
    raise_for_norma_status(response)

    check_response = _parse_checks_response(response)

    if _observability_enabled():
        try:
            server_events = _build_events_safely(
                check_response,
                request,
                {},
                resolved_trace_id,
                resolved_span_id,
                normalized_name,
            )
            _enqueue_events_safely(server_events)
        except Exception:
            _logger.warning("Rule event reporting could not prepare observations")

    return _as_checks_result(check_response)


async def evaluate_rules(
    step_name: str,
    *,
    input: Any | None = None,
    output: Any | None = None,
    context: dict[str, Any] | None = None,
    tools: list[dict[str, JSONValue]] | None = None,
    ground_truth: JSONValue | None = None,
    step_type: str = "llm",
    stage: Literal["pre", "post"] = "pre",
    agent_name: str,
    target_type: str | None = None,
    target_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> CheckResult:
    """Evaluate Rules for a step.

    When ``target_type`` and ``target_id`` are both supplied, the request
    is target-bearing: the server merges target bindings into the
    effective Rule set. If they are omitted, the SDK falls back to the
    target context fixed at ``init()`` time when present. A per-call
    override that disagrees with the session target is rejected because
    the session must retain a consistent target throughout its check lifecycle.
    """
    step_type = ensure_step_type(step_type)

    if state.server_url is None:
        raise RuntimeError("Server URL not configured. Call norma.init() first.")

    target_type, target_id = _resolve_session_target(target_type, target_id)

    default_value = {} if step_type == "tool" else ""
    step_dict: dict[str, Any] = {
        "type": step_type,
        "name": step_name,
        "input": input if input is not None else default_value,
        "output": output if output is not None else default_value,
    }
    if context is not None:
        step_dict["context"] = context
    if tools is not None:
        step_dict["tools"] = tools
    if ground_truth is not None:
        step_dict["ground_truth"] = ground_truth

    step_obj = Step(**step_dict)  # type: ignore[arg-type]
    resolved_rules = (
        state.require_rule_cache() if state.current_agent is not None else state.server_rules or []
    )

    async with NormaClient(
        runtime_url=state.server_url,
        agent_key=state.api_key,
        control_contract=state.control_contract,
    ) as client:
        result = await request_remote_check(
            client=client,
            agent_name=agent_name,
            step=step_obj,
            stage=stage,
            rules=resolved_rules,
            target_type=target_type,
            target_id=target_id,
            trace_id=trace_id,
            span_id=span_id,
            event_agent_name=agent_name,
        )
        if state.current_agent is not None:
            state.require_rule_cache()
        return result
