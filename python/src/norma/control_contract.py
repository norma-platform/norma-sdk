# Modified for Norma.
"""Opt-in negotiation for versioned control receipts and evidence."""

from __future__ import annotations

import re
from typing import Any, Literal

import httpx

ControlContract = Literal["rule-controls-v1"]
CONTROL_CONTRACT_HEADER = "X-Norma-Control-Contract"
CONTROL_CONTRACT = "rule-controls-v1"

_RULES_PATH = re.compile(r"/api/v1/agents/[^/]+/rules\Z")
_OPERATION_PATH = re.compile(
    r"/api/v1/operations(?:/[^/]+(?:/(?:observations|verification))?)?\Z"
)
_CHECK_RUN_PATH = re.compile(r"/api/v1/check-runs(?:/[^/]+)?\Z")


class UnsupportedControlContractError(RuntimeError):
    """The runtime did not prove it supports the requested control contract."""

    code = "CONTROL_CONTRACT_UNSUPPORTED"

    def __init__(self) -> None:
        super().__init__(
            "Unsupported Norma control contract: the runtime must echo "
            "rule-controls-v1 in both the response header and envelope."
        )


def is_control_contract_route(method: str, path: str) -> bool:
    """Return whether a public runtime route retains control-aware state/evidence."""
    method = method.upper()
    if method == "POST" and path in {
        "/api/v1/agents/register",
        "/api/v1/checks",
        "/api/v1/operations",
    }:
        return True
    if method in {"GET", "POST"} and _RULES_PATH.fullmatch(path):
        return True
    if path == "/api/v1/operations/observe-metrics":
        return False
    if method in {"GET", "POST"} and _OPERATION_PATH.fullmatch(path):
        return True
    if (method == "POST" and path == "/api/v1/check-runs/shadow") or (
        method == "GET" and _CHECK_RUN_PATH.fullmatch(path)
    ):
        return True
    return False


async def require_control_acknowledgement(response: httpx.Response) -> None:
    """Require the exact header and top-level envelope acknowledgement."""
    if response.headers.get(CONTROL_CONTRACT_HEADER) != CONTROL_CONTRACT:
        raise UnsupportedControlContractError()
    try:
        await response.aread()
        payload: Any = response.json()
    except (ValueError, httpx.ResponseNotRead):
        raise UnsupportedControlContractError() from None
    if not isinstance(payload, dict) or payload.get("control_contract") != CONTROL_CONTRACT:
        raise UnsupportedControlContractError()
