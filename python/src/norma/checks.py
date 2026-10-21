# Modified for Norma.
"""Explicit server Rule checks. The caller decides whether to run the action."""

from typing import Literal

from norma.models import Decision, Step

from ._checks import request_check as _request_check
from ._wire.adapters import decision_from_wire as _decision_from_wire
from .client import NormaClient


async def check(
    client: NormaClient,
    agent_name: str,
    step: Step,
    stage: Literal["pre", "post"],
    *,
    target_type: str | None = None,
    target_id: str | None = None,
) -> Decision:
    """Return a server Decision; inspect ``allowed`` before executing the action.

    This explicit helper performs server checks only. Use ``checkpoint`` for
    session-bound checks. SDK-local Rules are rejected by both entry points.
    """
    return _decision_from_wire(
        await _request_check(
            client,
            agent_name,
            step,
            stage,
            target_type=target_type,
            target_id=target_id,
        )
    )
