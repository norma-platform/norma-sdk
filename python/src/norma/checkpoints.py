# Modified for Norma.
"""
Rule decorator for server-side protection of agent functions.

This module provides a decorator that applies server-defined Rules to agent functions.
Rules can be associated via policies and direct norma links.

Architecture:
    SERVER defines: Policies -> Rules (stage, condition tree, action)
    SDK decorator: just marks WHERE Rules are evaluated

Usage:
    import norma

    norma.init(
        agent_name="my-agent-identity",
    )

    # Apply Rules associated with the agent
    @norma.checkpoint()
    async def chat(message: str) -> str:
        return await assistant.respond(message)

    # Server-side Rules define:
    # - stage: "pre" or "post"
    # - selector.path: "input" or "output"
    # - evaluator: regex, list, Luna evaluator, etc.
    # - action: deny, steer, or observe
"""

import asyncio
import functools
import inspect
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict, TypeVar

from norma._checks import (
    _resolve_session_target,
)
from norma._checks import (
    request_remote_check as _request_remote_check,
)
from norma._observed_state import StateSource, resolve_observed_state
from norma._state import state as _session_state
from norma._telemetry import get_trace_context_from_provider
from norma._wire.adapters import feedback_from_wire as _feedback_from_wire
from norma._wire.exports import (
    CheckResponse as _CheckResponse,
)
from norma.client import NormaClient
from norma.finish import ActionKind, ActionStatus, _record_decorated_action
from norma.models import Step, normalize_action, redact_turn_text
from norma.models.recovery_feedback import RecoveryFeedback
from norma.observability import (
    get_logger,
    log_rule_checks,
    log_span_end,
    log_span_start,
)
from norma.settings import get_settings
from norma.tracing import _generate_span_id, get_current_trace_id, get_trace_and_span_ids
from norma.validation import ensure_step_type

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def _resolve_rule_trace_context() -> tuple[str, str]:
    """Resolve trace/span IDs for a decorated Rule site.

    External providers, such as the Galileo bridge, are authoritative because
    they may reserve the concrete span ID that the eventual LLM/tool call will
    use. Without a provider, keep the existing behavior: share an active trace
    but create a fresh function span for this decorated call.
    """
    provider_context = get_trace_context_from_provider()
    if provider_context is not None:
        return provider_context["trace_id"], provider_context["span_id"]

    existing_trace_id = get_current_trace_id()
    if existing_trace_id:
        return existing_trace_id, _generate_span_id()

    return get_trace_and_span_ids()


@dataclass
class _CheckContext:
    """
    Holds state during Rule execution.

    This class encapsulates all the shared logic between async and sync
    Rule wrappers, including stats tracking, result processing, and logging.
    """

    agent_name: str
    server_url: str
    func: Callable
    args: tuple
    kwargs: dict
    trace_id: str
    span_id: str
    start_time: float
    step_name: str | None = None
    step_type: str | None = None
    action_kind: ActionKind | None = None
    action_target_arg: str | None = None
    action_command_arg: str | None = None
    action_cwd_arg: str | None = None

    # Stats (mutually exclusive: errors vs matches vs non_matches)
    total_executions: int = 0
    total_matches: int = 0
    total_non_matches: int = 0
    total_errors: int = 0
    total_unresolved: int = 0
    actions: dict[str, int] = field(default_factory=dict)

    def log_start(self) -> None:
        """Log span start."""
        log_span_start(self.trace_id, self.span_id, self.func.__name__, self.agent_name)

    def log_end(self) -> None:
        """Log span end with accumulated stats."""
        duration_ms = (time.perf_counter() - self.start_time) * 1000
        log_span_end(
            self.trace_id,
            self.span_id,
            self.func.__name__,
            duration_ms,
            executions=self.total_executions,
            matches=self.total_matches,
            non_matches=self.total_non_matches,
            errors=self.total_errors,
            unresolved=self.total_unresolved,
            actions=self.actions if self.actions else None,
        )

    def pre_payload(self) -> dict[str, Any]:
        """Build payload for pre-execution check (supports tool call detection)."""
        return _create_checks_payload(
            self.func,
            self.args,
            self.kwargs,
            output=None,
            step_name=self.step_name,
            explicit_step_type=self.step_type,
            action_kind=self.action_kind,
            action_target_arg=self.action_target_arg,
            action_command_arg=self.action_command_arg,
            action_cwd_arg=self.action_cwd_arg,
        )

    def post_payload(self, output: Any) -> dict[str, Any]:
        """Build payload for post-execution check (supports tool call detection)."""
        return _create_checks_payload(
            self.func,
            self.args,
            self.kwargs,
            output=output,
            step_name=self.step_name,
            explicit_step_type=self.step_type,
            action_kind=self.action_kind,
            action_target_arg=self.action_target_arg,
            action_command_arg=self.action_command_arg,
            action_cwd_arg=self.action_cwd_arg,
        )

    def process_result(
        self,
        result: dict[str, Any],
        check_stage: Literal["pre", "post"],
    ) -> None:
        """
        Process check result: log, handle actions, update stats.

        Args:
            result: Server check response
            check_stage: "pre" or "post"

        Raises:
            RuleViolationError: If any Rule triggers with "deny" action
            RuleSteerError: If any Rule triggers with "steer" action
        """
        # Log each Rule check
        _log_rule_checks(result, self.trace_id, self.span_id, check_stage)

        # Update stats in place
        self._update_stats(result)

        # Count blocking results too, before the existing exception signals stop execution.
        _handle_checks_result(result, check_stage)

    def _update_stats(self, result: dict[str, Any]) -> None:
        """
        Update stats in place from check result.

        The server returns three mutually exclusive lists:
        - matches: Rules where condition matched
        - non_matches: Rules that were evaluated but didn't match
        - errors: Rules that failed during check
        """
        # Process matches (Rules that matched)
        for match in result.get("matches") or []:
            self.total_executions += 1
            self.total_matches += 1
            action = normalize_action(str(
                "observe" if match.get("mode") == "observe"
                else match.get("effective_action") or match.get("action", "observe")
            ))
            self.actions[action] = self.actions.get(action, 0) + 1

        # Process non-matches (Rules evaluated but didn't match)
        for _ in result.get("non_matches") or []:
            self.total_executions += 1
            self.total_non_matches += 1

        # Process errors (Rules that failed check)
        for _ in result.get("errors") or []:
            self.total_executions += 1
            self.total_errors += 1

        for unresolved in result.get("unresolved") or []:
            self.total_executions += 1
            self.total_unresolved += 1
            action = normalize_action(str(unresolved.get("effective_action") or "observe"))
            self.actions[action] = self.actions.get(action, 0) + 1


class RuleViolationError(Exception):
    """A denied check; optional feedback describes the response, never permission to retry."""

    def __init__(
        self,
        rule_id: int | str | None = None,
        rule_name: str | None = None,
        message: str = "Rule violation",
        metadata: dict[str, Any] | None = None,
        *,
        stage: Literal["pre", "post"] | None = None,
        check_id: str | None = None,
        recovery_feedback: list[RecoveryFeedback] | None = None,
    ) -> None:
        self.rule_id = rule_id
        self.rule_name = rule_name or (str(rule_id) if rule_id else "unknown")
        self.message = message
        self.metadata = metadata or {}
        self.stage = stage
        self.check_id = check_id
        self.recovery_feedback = recovery_feedback
        super().__init__(f"Rule violation [{self.rule_name}]: {message}")


class RuleSteerError(Exception):
    """Raised when a Rule is triggered with 'steer' action.

    Execution at this checkpoint is blocked. The caller may choose a revised
    proposal and explicitly check it again; the SDK never retries the action.
    A post-stage failure occurs after the action has already run.
    """

    def __init__(
        self,
        rule_id: int | str | None = None,
        rule_name: str | None = None,
        message: str = "Rule steering required",
        metadata: dict[str, Any] | None = None,
        steering_context: str | None = None,
        input_data: Any | None = None,
        output_data: Any | None = None,
        *,
        stage: Literal["pre", "post"] | None = None,
        check_id: str | None = None,
        recovery_feedback: list[RecoveryFeedback] | None = None,
    ) -> None:
        self.rule_id = rule_id
        self.rule_name = rule_name or (str(rule_id) if rule_id else "unknown")
        self.message = message
        self.metadata = metadata or {}
        self.steering_context = steering_context or self.metadata.get(
            "steering_context", "No steering context provided"
        )
        self.input_data = input_data
        self.output_data = output_data
        self.stage = stage
        self.check_id = check_id
        self.recovery_feedback = recovery_feedback
        super().__init__(
            f"Rule steering [{self.rule_name}]: {message}\n"
            f"Steering context: {self.steering_context}"
        )


class CheckError(RuntimeError):
    """A failed check, compatible with existing ``RuntimeError`` handlers.

    Feedback and execution IDs are optional: transport failures have no response.
    At the post stage the action has already run; this exception is not a rollback.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: Literal["pre", "post"] | None = None,
        recovery_feedback: list[RecoveryFeedback] | None = None,
        check_ids: list[str] | None = None,
    ) -> None:
        self.stage = stage
        self.recovery_feedback = recovery_feedback
        self.check_ids = check_ids
        super().__init__(message)


class _FailureContext(TypedDict):
    stage: Literal["pre", "post"] | None
    recovery_feedback: list[RecoveryFeedback] | None


def _failure_context(
    result: dict[str, Any],
    stage: Literal["pre", "post"] | None,
) -> _FailureContext:
    """Preserve response feedback at the existing dictionary I/O boundary."""
    raw = result.get("recovery_feedback")
    return {
        "stage": stage,
        "recovery_feedback": (_feedback_from_wire(raw)),
    }


def _response_payload(result: _CheckResponse) -> dict[str, Any]:
    """Preserve the runtime-issued check identities when preparing internal diagnostics."""
    payload = result.model_dump(mode="json")
    for category in ("matches", "errors", "non_matches", "unresolved"):
        for match, serialized in zip(getattr(result, category) or [], payload.get(category) or []):
            if "check_id" not in match.model_fields_set:
                serialized.pop("check_id", None)
    return payload


def _get_current_agent() -> Any | None:
    """Get the current agent from norma module."""
    try:
        import norma

        return norma.current_agent()
    except Exception:
        return None


def _get_server_url() -> str:
    """Use the initialized session connection before standalone settings."""
    return _session_state.server_url or get_settings().runtime_url


async def _evaluate(
    agent_name: str,
    step: dict[str, Any],
    stage: Literal["pre", "post"],
    server_url: str,
    trace_id: str | None = None,
    span_id: str | None = None,
    rules: list[dict[str, Any]] | None = None,
    event_agent_name: str | None = None,
) -> dict[str, Any]:
    """Check the current hosted Rules before interpreting the remote decision."""
    target_type, target_id = _resolve_session_target(None, None)
    async with NormaClient(
        runtime_url=server_url,
        agent_key=_session_state.api_key,
        control_contract=_session_state.control_contract,
    ) as client:
        result = await _request_remote_check(
            client,
            agent_name,
            Step.model_validate(step),
            stage,
            rules or [],
            target_type=target_type,
            target_id=target_id,
            trace_id=trace_id,
            span_id=span_id,
        )
        return _response_payload(result)


def _unexpected_rule_failure_message(stage: str, error: Exception) -> str:
    """Build a user-facing message for unexpected Rule check failures."""
    if stage == "pre":
        return (
            "Rule check failed unexpectedly before execution. "
            f"Execution blocked for safety. Error: {error}"
        )
    return (
        f"Rule check failed unexpectedly after execution. Result blocked for safety. Error: {error}"
    )


async def _run_rule_check(
    ctx: _CheckContext,
    stage: Literal["pre", "post"],
    payload: dict[str, Any],
    rules: list[dict[str, Any]] | None,
) -> None:
    """Run one Rule stage and enforce fail-closed behavior on unexpected errors."""
    try:
        result = await _evaluate(
            ctx.agent_name,
            payload,
            stage,
            ctx.server_url,
            ctx.trace_id,
            ctx.span_id,
            rules=rules,
            event_agent_name=ctx.agent_name,
        )
        ctx.process_result(result, stage)
    except (RuleViolationError, RuleSteerError, CheckError):
        raise
    except Exception as e:
        stage_name = "Pre" if stage == "pre" else "Post"
        logger.error("%s-execution Rule check failed: %s", stage_name, e, exc_info=True)
        raise CheckError(
            _unexpected_rule_failure_message(stage, e),
            stage=stage,
        ) from e


def _extract_input_from_args(func: Callable, args: tuple, kwargs: dict) -> str:
    """
    Extract input data from function arguments.

    Tries common parameter names, then falls back to first string argument.
    """
    sig = inspect.signature(func)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()

    # Common input parameter names (in order of preference)
    input_names = ["input", "message", "query", "text", "prompt", "content", "user_input"]

    for name in input_names:
        if name in bound.arguments:
            value = bound.arguments[name]
            if value is not None:
                return str(value)

    # Fall back to first string argument
    for param_name, value in bound.arguments.items():
        if isinstance(value, str):
            return value

    # Last resort: stringify all arguments
    return str(bound.arguments)


def _create_checks_payload(
    func: Callable,
    args: tuple,
    kwargs: dict,
    output: Any = None,
    step_name: str | None = None,
    explicit_step_type: str | None = None,
    action_kind: ActionKind | None = None,
    action_target_arg: str | None = None,
    action_command_arg: str | None = None,
    action_cwd_arg: str | None = None,
) -> dict[str, Any]:
    """
    Create check payload for server, detecting if it's a tool step or LLM step.

    Returns a Step payload structure.

    Args:
        func: The function being evaluated
        args: Function positional arguments
        kwargs: Function keyword arguments
        output: Function output (None for pre-execution)
        step_name: Optional explicit step name to override auto-detection
        explicit_step_type: Optional explicit step type to override inference
    """
    sig = inspect.signature(func)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()

    # Determine step name priority: explicit step_name > tool_name > func.__name__
    if step_name:
        # Explicit step_name provided - use it
        determined_name = step_name
        # Try to detect if it's a tool based on attributes
        is_tool = (
            getattr(func, "name", None) is not None or getattr(func, "tool_name", None) is not None
        )
        inferred_type = "tool" if is_tool else "llm"
    else:
        # Auto-detect: Check if function has tool_name from @tool decorator
        tool_name = getattr(func, "name", None) or getattr(func, "tool_name", None)
        if tool_name:
            determined_name = tool_name
            inferred_type = "tool"
        else:
            determined_name = func.__name__
            inferred_type = "llm"

    step_type = (
        ensure_step_type(explicit_step_type) if explicit_step_type is not None else inferred_type
    )

    if step_type not in ("llm", "tool"):
        # Custom step types use the complete bound argument mapping. Unlike an
        # LLM step, there is no generic convention for selecting one argument
        # as the prompt/input for a retriever, trace, session, etc.
        return {
            "type": step_type,
            "name": determined_name,
            "input": dict(bound.arguments),
            "output": output
            if isinstance(output, (str, int, float, bool, dict, list))
            else (None if output is None else str(output)),
            "context": {"norma_argument_shape": "named"},
        }

    if step_type == "tool":
        # This is a tool step
        def bound_text(arg: str | None, limit: int) -> str | None:
            value = bound.arguments.get(arg) if arg is not None else None
            return redact_turn_text(value)[:limit] if isinstance(value, str) else None

        return {
            "type": step_type,
            "name": determined_name,
            "input": dict(bound.arguments),
            "output": output
            if isinstance(output, (str, int, float, bool, dict, list))
            else (None if output is None else str(output)),
            "context": {
                "norma_argument_shape": "named",
                "norma_action": {
                    "kind": action_kind or "other",
                    "target": bound_text(action_target_arg, 300)
                    if action_kind in {"shell", "edit"}
                    else (
                        bound_text(action_target_arg, 300)
                        or redact_turn_text(str(determined_name))[:300]
                    ),
                    "command": bound_text(action_command_arg, 500),
                    "cwd": bound_text(action_cwd_arg, 300),
                },
            },
        }

    # This is an LLM step
    input_data = _extract_input_from_args(func, args, kwargs)
    return {
        "type": step_type,
        "name": determined_name,
        "input": input_data,
        "output": output
        if isinstance(output, (str, int, float, bool, dict, list))
        else (None if output is None else str(output)),
    }


def _handle_checks_result(
    result: dict[str, Any],
    stage: Literal["pre", "post"] | None = None,
) -> None:
    """Handle check result from server - raise on deny or steer."""
    if not result or type(result.get("allowed")) is not bool:
        raise RuntimeError("Rule check did not return a valid safety decision")

    allowed = result["allowed"]
    matches = [item for item in result.get("matches") or []
               if not isinstance(item, dict) or item.get("mode", "enforce") != "observe"]
    # Keep uncertainty separate on the wire but apply its explicitly chosen action.
    matches.extend(
        {**item, "action": item.get("effective_action") or item.get("action") or "deny"}
        for item in result.get("unresolved") or []
        if isinstance(item, dict) and item.get("mode", "enforce") != "observe"
    )
    errors = [item for item in result.get("errors") or []
              if not isinstance(item, dict) or item.get("mode", "enforce") != "observe"]
    # Reported evaluator failures block regardless of the response bucket.
    errors.extend(
        item
        for category in ("matches", "non_matches", "unresolved")
        for item in result.get(category) or []
        if isinstance(item, dict)
        and isinstance(item.get("result"), dict)
        and item["result"].get("error") is not None
        and item.get("mode", "enforce") != "observe"
    )

    # CRITICAL: Check errors array FIRST - server-side failures must block execution
    if errors:
        error_messages = []
        for error in errors:
            if isinstance(error, dict):
                rule_name = error.get("rule_name", "unknown")
                result_data = error.get("result") or {}
                error_msg = (
                    result_data.get("error") or result_data.get("message") or "Unknown error"
                )
                error_messages.append(f"[{rule_name}] {error_msg}")

        blocked = (
            "Action already ran; result blocked for safety."
            if stage == "post"
            else "Execution blocked for safety."
        )
        raise CheckError(
            f"Rule check failed on server. {blocked}\nErrors: {'; '.join(error_messages)}",
            check_ids=[
                item["check_id"]
                for item in errors
                if isinstance(item, dict) and isinstance(item.get("check_id"), str)
            ]
            or None,
            **_failure_context(result, stage),
        )

    # CRITICAL: Handle actions in priority order: deny > steer > observe
    # Check blocking actions first (deny/steer), then log observe matches.

    if not allowed or any(
        isinstance(match, dict) and match.get("action") in {"deny", "steer"} for match in matches
    ):
        # Pass 1: Check for deny actions (highest priority - blocks immediately)
        for match in matches:
            if not isinstance(match, dict) or match.get("action", "deny") != "deny":
                continue

            result_data = match.get("result") or {}
            message = (
                result_data.get("message", "Rule triggered")
                if isinstance(result_data, dict)
                else "Rule triggered"
            )
            metadata = result_data.get("metadata", {}) if isinstance(result_data, dict) else {}
            raise RuleViolationError(
                rule_id=match.get("rule_id"),
                rule_name=match.get("rule_name", "unknown"),
                message=message,
                metadata=metadata,
                check_id=match.get("check_id"),
                **_failure_context(result, stage),
            )

        # Pass 2: Check for steer actions (second priority - signals correction needed)
        for match in matches:
            if not isinstance(match, dict) or match.get("action", "deny") != "steer":
                continue

            result_data = match.get("result") or {}
            message = (
                result_data.get("message", "Rule triggered")
                if isinstance(result_data, dict)
                else "Rule triggered"
            )
            metadata = result_data.get("metadata", {}) if isinstance(result_data, dict) else {}

            # Extract steering_context directly from match (now a first-class field)
            steering_context_obj = match.get("steering_context")

            # Extract message from SteeringContext object or use as string
            if isinstance(steering_context_obj, dict):
                steering_context = steering_context_obj.get("message", message)
            elif isinstance(steering_context_obj, str):
                steering_context = steering_context_obj
            else:
                # No steering context provided, use evaluator message
                steering_context = message

            raise RuleSteerError(
                rule_id=match.get("rule_id"),
                rule_name=match.get("rule_name", "unknown"),
                message=message,
                metadata=metadata,
                steering_context=steering_context,
                check_id=match.get("check_id"),
                **_failure_context(result, stage),
            )

        raise RuleViolationError(
            message="Rule check returned a blocked decision",
            **_failure_context(result, stage),
        )

    # Log observe actions (non-blocking, always processed).
    for match in result.get("matches") or []:
        if not isinstance(match, dict):
            continue

        action = normalize_action(str(
                "observe" if match.get("mode") == "observe"
                else match.get("effective_action") or match.get("action", "observe")
            ))
        if action != "observe":
            continue

        result_data = match.get("result") or {}
        message = (
            result_data.get("message", "Rule triggered")
            if isinstance(result_data, dict)
            else "Rule triggered"
        )
        rule_name = match.get("rule_name", "unknown")

        logger.info(f"Rule observe [{rule_name}]: {message}")


def _log_rule_checks(
    result: dict[str, Any],
    trace_id: str,
    span_id: str,
    check_stage: str,
) -> None:
    """Log each Rule check from the result for debugging."""
    # Log matches
    for match in result.get("matches") or []:
        _log_single_rule(match, trace_id, span_id, check_stage, matched=True)

    # Log errors
    for error in result.get("errors") or []:
        _log_single_rule(error, trace_id, span_id, check_stage, matched=False)

    # Log non-matches
    for non_match in result.get("non_matches") or []:
        _log_single_rule(non_match, trace_id, span_id, check_stage, matched=False)

    for unresolved in result.get("unresolved") or []:
        _log_single_rule(unresolved, trace_id, span_id, check_stage, matched=False)


def _log_single_rule(
    rule_data: dict[str, Any],
    trace_id: str,
    span_id: str,
    check_stage: str,
    matched: bool,
) -> None:
    """Log a single Rule check."""
    log_rule_checks(
        trace_id=trace_id,
        span_id=span_id,
        rule_name=rule_data.get("rule_name", "unknown"),
        matched=matched,
        action=normalize_action(
            str(rule_data.get("effective_action") or rule_data.get("action", "observe"))
        ),
        confidence=rule_data.get("result", {}).get("confidence", 0.0),
        duration_ms=rule_data.get("result", {}).get("execution_duration_ms"),
        check_id=rule_data.get("check_id"),
        check_stage=check_stage,
        outcome=rule_data.get("result", {}).get("outcome"),
    )


def _get_server_rules() -> list[dict[str, Any]] | None:
    """Get the cached server Rules from norma module."""
    if _session_state.current_agent is not None:
        return _session_state.require_rule_cache()
    return _session_state.server_rules


async def _execute_with_rule(
    func: Callable,
    args: tuple,
    kwargs: dict,
    is_async: bool,
    step_name: str | None = None,
    step_type: str | None = None,
    action_kind: ActionKind | None = None,
    action_target_arg: str | None = None,
    action_command_arg: str | None = None,
    action_cwd_arg: str | None = None,
    state: StateSource | None = None,
) -> Any:
    """
    Core Rule execution logic for both async and sync functions.

    This function is always called in an async context (either directly with await
    for async functions, or inside asyncio.run() for sync functions), so it can
    always use await _evaluate() directly.

    Uses the initialized hosted Rule snapshot and rejects unsupported local Rules.
    Every check is evaluated remotely before execution may proceed.

    Args:
        func: The wrapped function to execute
        args: Positional arguments for the function
        kwargs: Keyword arguments for the function
        is_async: Whether the wrapped function is async
        step_name: Optional explicit step name for Rule matching
        step_type: Optional explicit step type for Rule matching

    Returns:
        The result of the wrapped function

    Raises:
        RuleViolationError: If any Rule triggers with "deny" action
        RuleSteerError: If any Rule triggers with "steer" action
        RuntimeError: If Rule check fails unexpectedly
    """
    agent = _get_current_agent()
    if agent is None:
        raise CheckError(
            "Norma is not initialized; checkpoint execution blocked. "
            "Call norma.init() successfully before running the agent."
        )

    session_bound = _session_state.current_agent is agent

    # Detach caller-owned nested values before the first asynchronous check.
    # Keep an instance/class method receiver: copying it would execute against a
    # different object. All other explicit arguments and defaults use Python's
    # copy protocol; failures block rather than running unchecked arguments.
    signature = inspect.signature(func)
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    first_parameter = next(iter(signature.parameters), None)
    memo = {}
    if first_parameter in {"self", "cls"} and first_parameter in bound.arguments:
        receiver = bound.arguments[first_parameter]
        memo[id(receiver)] = receiver
    try:
        bound.arguments = deepcopy(bound.arguments, memo)
    except Exception as exc:
        raise RuntimeError("Rule arguments could not be copied; execution blocked") from exc
    args, kwargs = bound.args, bound.kwargs

    # Resolve application state once. The provider receives a separate argument
    # copy so mutating it cannot change the action approved by the checkpoint.
    observed_state = None
    if state is not None:
        try:
            state_args, state_kwargs = deepcopy((args, kwargs), memo)
            observed_state = await resolve_observed_state(state, state_args, state_kwargs)
        except Exception:
            # Provider exceptions can contain credentials or application objects.
            raise CheckError(
                "Checkpoint state could not be captured as bounded JSON; execution blocked.",
                stage="pre",
            ) from None

    # Get cached Rules to detect unsupported local Rules
    rules = _get_server_rules()

    trace_id, span_id = _resolve_rule_trace_context()

    ctx = _CheckContext(
        agent_name=agent.agent_name,
        server_url=_get_server_url(),
        func=func,
        args=args,
        kwargs=kwargs,
        trace_id=trace_id,
        span_id=span_id,
        start_time=time.perf_counter(),
        step_name=step_name,
        step_type=step_type,
        action_kind=action_kind,
        action_target_arg=action_target_arg,
        action_command_arg=action_command_arg,
        action_cwd_arg=action_cwd_arg,
    )
    ctx.log_start()

    ran = False
    completed = False
    output: Any = None
    action_status: ActionStatus = "unknown"
    action_error: BaseException | None = None
    pre_payload: dict[str, Any] | None = None
    try:
        # PRE-EXECUTION: Check Rules with check_stage="pre"
        pre_payload = deepcopy(ctx.pre_payload())
        if observed_state is not None:
            pre_payload.setdefault("context", {})["state"] = deepcopy(observed_state)
        await _run_rule_check(ctx, "pre", deepcopy(pre_payload), rules)

        if session_bound and _session_state.current_agent is not agent:
            raise CheckError(
                "Norma session changed during the check; execution blocked. "
                "Initialize successfully and check the action again."
            )

        # A refresh can fail while the precheck is awaiting the runtime. Do not
        # execute with newly unknown local coverage after that failure.
        if _session_state.current_agent is not None:
            _session_state.require_rule_cache()

        # Execute the function
        ran = True
        if is_async:
            output = await func(*args, **kwargs)
        else:
            output = func(*args, **kwargs)
        completed = True
        action_status = "succeeded"

        # POST-EXECUTION: Check Rules with check_stage="post"
        post_payload = ctx.post_payload(output)
        # The action may mutate its private arguments. Post checks still identify
        # the input approved at preflight, alongside the actual returned output.
        post_payload["input"] = deepcopy(pre_payload["input"])
        if observed_state is not None:
            post_payload.setdefault("context", {})["state"] = deepcopy(observed_state)
        await _run_rule_check(ctx, "post", deepcopy(post_payload), rules)

        return output
    except (RuleViolationError, RuleSteerError) as exc:
        action_status = "succeeded" if completed else ("failed" if ran else "denied")
        action_error = exc
        raise
    except Exception as exc:
        action_status = "succeeded" if completed else ("failed" if ran else "unknown")
        action_error = exc
        raise
    finally:
        action_context = (
            pre_payload.get("context", {}).get("norma_action", {})
            if pre_payload is not None
            else {}
        )
        _record_decorated_action(
            str(pre_payload["name"]) if pre_payload is not None else func.__name__,
            action_status,
            kind=action_context.get("kind", "other"),
            target=action_context.get("target"),
            command=action_context.get("command"),
            result=output if completed else None,
            error=action_error if not completed else None,
            duration_ms=int((time.perf_counter() - ctx.start_time) * 1000),
        )
        ctx.log_end()


def _checkpoint_decorator(
    policy: str | None = None,
    step_name: str | None = None,
    step_type: str | None = None,
    action_kind: ActionKind | None = None,
    action_target_arg: str | None = None,
    action_command_arg: str | None = None,
    action_cwd_arg: str | None = None,
    state: StateSource | None = None,
) -> Callable[[F], F]:
    """Implement checkpoint wrapping; initialization and remote checks are mandatory.

    The public checkpoint wrapper also validates the initialized Rule cache.
    This helper cannot execute an action without an initialized agent.
    """
    if action_kind not in {None, "shell", "edit", "read", "search", "web", "mcp", "task", "other"}:
        raise ValueError("action_kind must be a normalized action kind")
    if step_type is not None:
        step_type = ensure_step_type(step_type)

    # The policy parameter is for documentation only - the server evaluates
    # Rules associated with the agent via policy and direct links.
    _ = policy

    def decorator(func: F) -> F:
        # Register this function's step schema for auto-discovery by init()
        from norma._rule_registry import register

        register(func, policy=policy, step_type=step_type, step_name=step_name)

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _execute_with_rule(
                func,
                args,
                kwargs,
                is_async=True,
                step_name=step_name,
                step_type=step_type,
                action_kind=action_kind,
                action_target_arg=action_target_arg,
                action_command_arg=action_command_arg,
                action_cwd_arg=action_cwd_arg,
                state=state,
            )

        # Copy over ALL attributes from the original function (important for LangChain tools)
        for attr in dir(func):
            if not attr.startswith("_") and attr not in ("__call__", "__wrapped__"):
                try:
                    setattr(async_wrapper, attr, getattr(func, attr))
                except (AttributeError, TypeError):
                    pass

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return asyncio.run(
                _execute_with_rule(
                    func,
                    args,
                    kwargs,
                    is_async=False,
                    step_name=step_name,
                    step_type=step_type,
                    action_kind=action_kind,
                    action_target_arg=action_target_arg,
                    action_command_arg=action_command_arg,
                    action_cwd_arg=action_cwd_arg,
                    state=state,
                )
            )

        if inspect.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper  # type: ignore

    return decorator


def checkpoint(
    step_name: str | None = None,
    step_type: str = "tool",
    *,
    action_kind: ActionKind | None = None,
    action_target_arg: str | None = None,
    action_command_arg: str | None = None,
    action_cwd_arg: str | None = None,
    state: StateSource | None = None,
) -> Callable[[F], F]:
    """Mark a Rule checkpoint; block execution until initialization succeeds.

    Define checkpoints before calling ``norma.init()`` so their schemas are
    registered. A pre-stage denial prevents execution; a post-stage denial
    blocks return after execution and does not roll the action back.

    ``state`` is an optional plain JSON object or sync/async callback accepting
    the function's arguments. It is resolved and copied once before the precheck;
    both stages evaluate that same snapshot at ``context.state``. Supply only
    intended facts, not application objects or secrets. State is evaluated even
    when server retention is disabled; retention is configured separately.
    """
    decorate = _checkpoint_decorator(
        step_name=step_name,
        step_type=step_type,
        action_kind=action_kind,
        action_target_arg=action_target_arg,
        action_command_arg=action_command_arg,
        action_cwd_arg=action_cwd_arg,
        state=state,
    )

    def require_ready() -> None:
        if _session_state.current_agent is None:
            raise CheckError(
                "Norma is not initialized; checkpoint execution blocked. "
                "Call norma.init() successfully before running the agent."
            )
        try:
            _session_state.require_rule_cache()
        except RuntimeError as exc:
            raise CheckError(str(exc)) from exc

    def decorator(func: F) -> F:
        guarded = decorate(func)
        if inspect.iscoroutinefunction(func):

            @functools.wraps(guarded)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                require_ready()
                return await guarded(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(guarded)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            require_ready()
            return guarded(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = ["checkpoint", "RuleViolationError", "RuleSteerError", "CheckError"]
