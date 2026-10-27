# Modified for Norma.
"""Aggregate native check outcomes into a typed Decision."""

from typing import Any

from ..models.decisions import Decision
from ..models.recovery_feedback import RecoveryFeedback
from ..models.rules import RuleMatch
from .checks import CheckResponse


def feedback_from_wire(items: object) -> list[RecoveryFeedback] | None:
    if items is None:
        return None
    if not isinstance(items, list):
        raise ValueError("Invalid recovery feedback")
    results = []
    for item in items:
        data = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        results.append(RecoveryFeedback.model_validate(data))
    return results


def decision_from_wire(value: CheckResponse | dict[str, Any]) -> Decision:
    response = value if isinstance(value, CheckResponse) else CheckResponse.model_validate(value)
    checks = []
    for group, status in (
        (response.matches, "matched"),
        (response.non_matches, "not_matched"),
        (response.errors, "error"),
        (response.unresolved, "unresolved"),
    ):
        for match in group or []:
            data = match.model_dump(mode="json")
            data["status"] = status
            checks.append(RuleMatch.model_validate(data))
    return Decision(
        allowed=response.allowed,
        reason=response.reason,
        checks=checks,
        execution_contract=response.execution_contract,
        recovery_feedback=feedback_from_wire(response.recovery_feedback),
        control_contract=response.control_contract,
        agent_generation=response.agent_generation,
        control_resolutions=response.control_resolutions,
        control_evidence=response.control_evidence,
    )
