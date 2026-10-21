# Modified for Norma.
"""Shared enforcement helpers for framework integrations."""

from __future__ import annotations

from typing import Any, Literal

import norma
from norma import RuleSteerError, RuleViolationError
from norma.models import Decision, JSONValue


def _action_error(
    result: Decision, *, stage: Literal["pre", "post"] | None = None
) -> tuple[str, Exception] | None:
    """Return the first blocking action from an check result."""

    matches = [
        check
        for check in result.checks
        if getattr(check, "mode", "enforce") != "observe"
        and check.status in ("matched", "unresolved")
    ]
    deny_match = next(
        (m for m in matches if (getattr(m, "effective_action", None) or m.action) == "deny"), None
    )
    if deny_match:
        msg = getattr(getattr(deny_match, "result", None), "message", None) or result.reason
        msg = msg or f"Rule '{deny_match.rule_name}' triggered"
        deny_err = RuleViolationError(
            rule_id=deny_match.rule_id,
            rule_name=deny_match.rule_name,
            message=msg,
            metadata=getattr(deny_match.result, "metadata", None),
            check_id=getattr(deny_match, "check_id", None),
            stage=stage,
            recovery_feedback=getattr(result, "recovery_feedback", None),
        )
        return "deny", deny_err

    steer_match = next(
        (m for m in matches if (getattr(m, "effective_action", None) or m.action) == "steer"), None
    )
    if not steer_match:
        return None

    msg = getattr(getattr(steer_match, "result", None), "message", None) or result.reason
    msg = msg or f"Rule '{steer_match.rule_name}' triggered"
    ctx = getattr(steer_match, "steering_context", None)
    ctx_msg = getattr(ctx, "message", None) if ctx else None
    steer_err = RuleSteerError(
        rule_id=steer_match.rule_id,
        rule_name=steer_match.rule_name,
        message=f"Steering required [{steer_match.rule_name}]: {msg}",
        metadata=getattr(steer_match.result, "metadata", None),
        steering_context=ctx_msg or msg,
        check_id=getattr(steer_match, "check_id", None),
        stage=stage,
        recovery_feedback=getattr(result, "recovery_feedback", None),
    )
    return "steer", steer_err


async def _evaluate_and_enforce(
    agent_name: str,
    step_name: str,
    *,
    input: Any | None = None,
    output: Any | None = None,
    context: dict[str, Any] | None = None,
    tools: list[dict[str, JSONValue]] | None = None,
    ground_truth: JSONValue | None = None,
    step_type: str = "llm",
    stage: Literal["pre", "post"] = "pre",
) -> Decision:
    """Evaluate rules and enforce fail-closed blocking semantics."""

    result = await norma.evaluate_rules(
        step_name=step_name,
        input=input,
        output=output,
        context=context,
        tools=tools,
        ground_truth=ground_truth,
        step_type=step_type,
        stage=stage,
        agent_name=agent_name,
    )

    errors = [
        check
        for check in result.checks
        if getattr(check, "mode", "enforce") != "observe"
        and (check.status == "error" or getattr(check.result, "error", None) is not None)
    ]
    if errors:
        error_names = ", ".join(check.rule_name for check in errors)
        raise RuntimeError(
            f"Rule check failed; execution blocked for safety. Errors: {error_names or 'unknown'}"
        )

    action = _action_error(result, stage=stage)
    if action:
        _, err = action
        raise err

    if not result.allowed:
        rule_name = "unknown"
        reason = result.reason
        match = None

        if [
            check
            for check in result.checks
            if getattr(check, "mode", "enforce") != "observe"
            and check.status in ("matched", "unresolved")
        ]:
            match = [
                check
                for check in result.checks
                if getattr(check, "mode", "enforce") != "observe"
                and check.status in ("matched", "unresolved")
            ][0]
            rule_name = match.rule_name
            if not reason:
                match_result = getattr(match, "result", None)
                msg = getattr(match_result, "message", None) if match_result else None
                reason = msg or f"Rule '{rule_name}' triggered"

        raise RuleViolationError(
            rule_id=(match.rule_id if match is not None else None),
            rule_name=rule_name,
            message=reason or "Rule violation",
            metadata=(getattr(match.result, "metadata", None) if match is not None else None),
            check_id=getattr(match, "check_id", None),
            stage=stage,
            recovery_feedback=result.recovery_feedback,
        )

    return result
