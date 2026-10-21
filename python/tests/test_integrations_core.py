# Modified for Norma.
"""Unit tests for shared framework integration helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from norma import RuleSteerError, RuleViolationError
from norma.integrations._core import _action_error, _evaluate_and_enforce
from norma.models import Decision


def _match(
    *,
    action: str,
    status: str = "matched",
    rule_name: str = "test-rule",
    rule_id: int = 1,
    message: str = "blocked",
    steering_context: str | None = None,
) -> SimpleNamespace:
    ctx = SimpleNamespace(message=steering_context) if steering_context is not None else None
    return SimpleNamespace(
        action=action,
        status=status,
        rule_name=rule_name,
        rule_id=rule_id,
        result=SimpleNamespace(message=message, metadata={"source": "test"}),
        steering_context=ctx,
    )


def test_action_error_returns_deny():
    result = MagicMock(spec=Decision)
    result.checks = [_match(action="deny", message="Denied")]
    result.reason = None

    action, err = _action_error(result) or ("", Exception())

    assert action == "deny"
    assert isinstance(err, RuleViolationError)
    assert err.rule_name == "test-rule"


def test_action_error_returns_steer():
    result = MagicMock(spec=Decision)
    result.checks = [_match(action="steer", message="Steer", steering_context="Rewrite it")]
    result.reason = None

    action, err = _action_error(result) or ("", Exception())

    assert action == "steer"
    assert isinstance(err, RuleSteerError)
    assert err.steering_context == "Rewrite it"


@pytest.mark.asyncio
async def test_evaluate_and_enforce_returns_safe_result():
    safe_result = MagicMock(spec=Decision)
    safe_result.allowed = True
    safe_result.checks = []

    with patch(
        "norma.integrations._core.norma.evaluate_rules",
        AsyncMock(return_value=safe_result),
    ) as mock_evaluate:
        result = await _evaluate_and_enforce(
            "test-agent01",
            "writer",
            input="hi",
            step_type="llm",
            stage="pre",
        )

    assert result is safe_result
    mock_evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluate_and_enforce_fails_closed_on_errors():
    errored = MagicMock(spec=Decision)
    errored.allowed = True
    errored.checks = []
    errored.checks = [_match(action="deny", rule_name="bad-rule", status="error")]

    with patch(
        "norma.integrations._core.norma.evaluate_rules",
        AsyncMock(return_value=errored),
    ):
        with pytest.raises(RuntimeError, match="Rule check failed"):
            await _evaluate_and_enforce("test-agent01", "writer")


@pytest.mark.asyncio
async def test_evaluate_and_enforce_raises_deny_first():
    result = MagicMock(spec=Decision)
    result.allowed = False
    result.checks = [
        _match(action="steer", rule_name="steer-rule", steering_context="Rewrite"),
        _match(action="deny", rule_name="deny-rule", message="Nope"),
    ]
    result.reason = None

    with patch(
        "norma.integrations._core.norma.evaluate_rules",
        AsyncMock(return_value=result),
    ):
        with pytest.raises(RuleViolationError) as exc_info:
            await _evaluate_and_enforce("test-agent01", "writer")

    assert exc_info.value.rule_name == "deny-rule"


@pytest.mark.asyncio
async def test_evaluate_and_enforce_raises_steer():
    result = MagicMock(spec=Decision)
    result.allowed = False
    result.checks = [_match(action="steer", message="rewrite", steering_context="Try again")]
    result.reason = None

    with patch(
        "norma.integrations._core.norma.evaluate_rules",
        AsyncMock(return_value=result),
    ):
        with pytest.raises(RuleSteerError, match="Try again"):
            await _evaluate_and_enforce("test-agent01", "writer")


@pytest.mark.asyncio
async def test_evaluate_and_enforce_fallback_violation_includes_rule_id():
    result = MagicMock(spec=Decision)
    result.allowed = False
    result.checks = [_match(action="observe", rule_id=42, message="unsafe")]
    result.recovery_feedback = None
    result.reason = None

    with patch(
        "norma.integrations._core.norma.evaluate_rules",
        AsyncMock(return_value=result),
    ):
        with pytest.raises(RuleViolationError) as exc_info:
            await _evaluate_and_enforce("test-agent01", "writer")

    assert exc_info.value.rule_id == 42


@pytest.mark.parametrize("action", ["deny", "steer"])
async def test_unresolved_blocking_decision_is_enforced(action: str) -> None:
    # Given: the runtime explicitly chooses a blocking action for uncertain evidence.
    result = Decision.model_validate(
        {
            "allowed": True,
            "checks": [
                {
                    "rule_id": 1,
                    "rule_name": "evidence",
                    "status": "unresolved",
                    "action": "observe",
                    "effective_action": action,
                    "result": {"matched": False, "confidence": 0},
                }
            ],
        }
    )
    with patch("norma.integrations._core.norma.evaluate_rules", new=AsyncMock(return_value=result)):
        # When: a framework checks the proposed tool invocation.
        with pytest.raises((RuleViolationError, RuleSteerError)):
            await _evaluate_and_enforce("agent", "write", step_type="tool")
    # Then: uncertainty does not permit an action selected for blocking.
    assert not result.allowed


@pytest.mark.parametrize("status", ["matched", "not_matched", "unresolved", "error"])
async def test_embedded_error_precedes_steering(status: str) -> None:
    result = Decision.model_validate(
        {
            "allowed": False,
            "checks": [
                {
                    "rule_id": 1,
                    "rule_name": "steer",
                    "status": "matched",
                    "action": "steer",
                    "result": {"matched": True, "confidence": 1},
                },
                {
                    "rule_id": 2,
                    "rule_name": "failed",
                    "status": status,
                    "action": "observe",
                    "result": {"matched": False, "confidence": 0, "error": "unavailable"},
                },
            ],
        }
    )
    with patch("norma.integrations._core.norma.evaluate_rules", new=AsyncMock(return_value=result)):
        with pytest.raises(RuntimeError, match="Rule check failed"):
            await _evaluate_and_enforce("agent", "write")
