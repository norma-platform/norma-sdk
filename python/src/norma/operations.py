# Modified for Norma.
"""Durable operation decisions and caller-reported effects."""

from urllib.parse import quote

from norma.models.operations import (
    AddOperationObservationRequest,
    CreateOperationRequest,
    LinkOperationVerificationRequest,
    OperationListResponse,
    OperationRecord,
)

from ._wire.adapters import decision_from_wire as _decision_from_wire
from ._wire.operations import OperationRecord as _WireOperationRecord
from .client import NormaClient
from .errors import raise_for_norma_status


async def create(
    client: NormaClient,
    request: CreateOperationRequest,
) -> OperationRecord:
    """Evaluate and retain one proposed operation and its frozen decision."""
    request = CreateOperationRequest.model_validate(request.model_dump(mode="python"))
    response = await client._request_runtime_resource(
        "POST",
        "/api/v1/operations",
        json=request.model_dump(mode="json"),
        target_type=request.target_type,
        target_id=request.target_id,
    )
    raise_for_norma_status(response)
    wire_record = _WireOperationRecord.model_validate(response.json())
    record = _public_record(wire_record)
    if record.decision.execution_contract != "hosted-v1":
        raise RuntimeError("Unsupported hosted runtime: execution_contract hosted-v1 is required.")
    if wire_record.request.model_dump(mode="json") != request.model_dump(mode="json"):
        raise RuntimeError("Operation receipt does not match the submitted proposal")
    _require_identity(
        wire_record, wire_record.id, request.agent_name, request.target_type, request.target_id
    )
    if any(
        item.definition.enabled and item.definition.execution != "server"
        for item in wire_record.rules
    ):
        raise RuntimeError("Hosted operation cannot evaluate SDK-local Rules")
    return record


async def list_for_agent(
    client: NormaClient,
    agent_name: str,
    *,
    limit: int = 50,
    target_type: str | None = None,
    target_id: str | None = None,
) -> OperationListResponse:
    """List retained operations for an agent, newest first."""
    params = {"agent_name": agent_name, "limit": str(limit)}
    if target_type is not None and target_id is not None:
        params["target_type"] = target_type
        params["target_id"] = target_id
    response = await client._request_runtime_resource(
        "GET",
        "/api/v1/operations",
        params=params,
        target_type=target_type,
        target_id=target_id,
    )
    raise_for_norma_status(response)
    records = []
    for item in response.json()["operations"]:
        record = _WireOperationRecord.model_validate(item)
        _require_identity(record, record.id, agent_name, target_type, target_id)
        records.append(_public_record(record))
    return OperationListResponse(operations=records)


async def get(
    client: NormaClient,
    operation_id: str,
    *,
    agent_name: str,
    target_type: str | None = None,
    target_id: str | None = None,
) -> OperationRecord:
    """Read one operation in its agent namespace."""
    params = {"agent_name": agent_name}
    if target_type is not None and target_id is not None:
        params["target_type"] = target_type
        params["target_id"] = target_id
    response = await client._request_runtime_resource(
        "GET",
        f"/api/v1/operations/{quote(operation_id, safe='')}",
        params=params,
        target_type=target_type,
        target_id=target_id,
    )
    raise_for_norma_status(response)
    wire_record = _WireOperationRecord.model_validate(response.json())
    _require_identity(wire_record, operation_id, agent_name, target_type, target_id)
    return _public_record(wire_record)


async def link_verification(
    client: NormaClient,
    operation_id: str,
    request: LinkOperationVerificationRequest,
) -> OperationRecord:
    """Link a separately assessed postcondition to the existing operation."""
    response = await client._request_runtime_resource(
        "POST",
        f"/api/v1/operations/{quote(operation_id, safe='')}/verification",
        json=request.model_dump(mode="json"),
        target_type=request.target_type,
        target_id=request.target_id,
    )
    raise_for_norma_status(response)
    wire_record = _WireOperationRecord.model_validate(response.json())
    _require_identity(
        wire_record, operation_id, request.agent_name, request.target_type, request.target_id
    )
    return _public_record(wire_record)


async def observe(
    client: NormaClient,
    operation_id: str,
    request: AddOperationObservationRequest,
) -> OperationRecord:
    """Append a caller-reported effect while preserving the original decision."""
    response = await client._request_runtime_resource(
        "POST",
        f"/api/v1/operations/{quote(operation_id, safe='')}/observations",
        json=request.model_dump(mode="json"),
        target_type=request.target_type,
        target_id=request.target_id,
    )
    raise_for_norma_status(response)
    wire_record = _WireOperationRecord.model_validate(response.json())
    _require_identity(
        wire_record, operation_id, request.agent_name, request.target_type, request.target_id
    )
    return _public_record(wire_record)


def _public_record(record: _WireOperationRecord) -> OperationRecord:
    """Keep native frozen receipts readable with typed Decision aggregation."""
    data = record.model_dump(mode="json")
    for rule in data["rules"]:
        supported = rule["definition"].get("execution") == "server"
        rule["status"] = "hosted" if supported else "unsupported"
        rule["definition"] = rule["definition"] if supported else None
    decision = _decision_from_wire(record.decision.check).model_dump(mode="json")
    if record.decision.outcome == "allow":
        check = record.decision.check
        reported_errors = any(item.mode == "enforce" for item in check.errors or []) or any(
            check.mode == "enforce" and check.result.error is not None
            for group in (check.matches, check.non_matches, check.unresolved)
            for check in group or []
        )
        blocking_action = any(
            check.mode == "enforce"
            and (check.effective_action or check.action) in {"deny", "steer"}
            for group in (check.matches, check.unresolved)
            for check in group or []
        )
        observed_advisory = any(
            check.result.matched
            and check.result.error is None
            and (check.effective_action or check.action) == "observe"
            for check in check.matches or []
        )
        allowed_unresolved = (any(item.mode == "enforce" for item in check.unresolved or [])
                              and record.request.unresolved_policy == "allow")
        if (
            not check.allowed
            and not reported_errors
            and not observed_advisory
            and not allowed_unresolved
        ):
            raise RuntimeError("Operation receipt has an unexplained blocked check")
        if blocking_action or (reported_errors and record.request.error_policy == "block"):
            raise RuntimeError("Operation receipt contradicts its retained enforcement policy")
        if (any(item.mode == "enforce" for item in check.unresolved or [])
                and record.request.unresolved_policy == "block"):
            raise RuntimeError("Operation receipt contradicts its retained unresolved policy")
        # Operation outcome incorporates its retained policy; historical error allowances
        # remain readable but a failed check never becomes current permission.
        decision["allowed"] = not reported_errors
    decision["outcome"] = record.decision.outcome
    decision["reason"] = record.decision.reason
    data["decision"] = decision
    return OperationRecord.model_validate(data)


def _require_identity(
    record: _WireOperationRecord,
    operation_id: str,
    agent_name: str,
    target_type: str | None,
    target_id: str | None,
) -> None:
    if (record.id, record.agent_name, record.target_type, record.target_id) != (
        operation_id,
        agent_name,
        target_type,
        target_id,
    ):
        raise RuntimeError("Operation receipt does not match the requested identity")
    if (
        record.request.agent_name,
        record.request.operation_key,
        record.request.target_type,
        record.request.target_id,
    ) != (
        record.agent_name,
        record.operation_key,
        record.target_type,
        record.target_id,
    ):
        raise RuntimeError("Operation receipt contains inconsistent proposal identity")
