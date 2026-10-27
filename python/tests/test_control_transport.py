# Modified for Norma.
"""Public SDK contract negotiation and evidence readback tests."""

import httpx
import pytest
from pydantic import ValidationError

from norma import UnsupportedControlContractError
from norma._check_events import build_rule_check_events
from norma._wire.adapters import decision_from_wire
from norma._wire.checks import CheckRequest, CheckResponse
from norma.agents import (
    _list_agent_rules,
    _register_agent,
    list_agent_rules_typed,
    register_agent,
)
from norma.client import NormaClient
from norma.control_contract import is_control_contract_route
from norma.models import Agent, Step
from norma.models.control_evidence import (
    ControlEvidenceSnapshot,
    ControlReceipt,
    NumericComparisonEvidence,
)

URL = "https://runtime.example.test/api/runtime"


def _rule_readback_payload() -> dict[str, object]:
    receipt = {
        "contract": "rule-controls-v1",
        "capabilities_fingerprint": "a" * 64,
        "declaration_fingerprint": "b" * 64,
        "defaults": {"age": 90},
        "effective_values": {"age": 120},
        "values_fingerprint": "c" * 64,
        "value_sources": {"age": "agent_override"},
        "references": [{
            "key": "age", "source_pointer": "/controls/age/default",
            "destination_pointer": "/condition/evaluator/config/field_constraints/age/min",
            "evaluator_pointer": "/condition/evaluator", "slot": {"control_types": ["number"]},
        }],
        "overridden_keys": ["age"],
        "agent_generation": "generation-1",
        "agent_name": "hosted-test-agent",
        "rule_id": 7,
        "rule_version": 3,
        "override_revision": 4,
        "effective_fingerprint": "d" * 64,
    }
    return {
        "rules": [{
            "id": 7,
            "name": "age-check",
            "rule": {
                "execution": "server",
                "condition": {
                    "evaluator": {
                        "name": "json",
                        "config": {"field_constraints": {"age": {"min": 120}}},
                    }
                },
                "action": {"decision": "deny"},
            },
        }],
        "control_contract": "rule-controls-v1",
        "agent_generation": "generation-1",
        "control_resolutions": [receipt],
    }


async def test_public_agent_rule_list_and_register_preserve_control_receipt() -> None:
    payload = _rule_readback_payload()
    headers = {"X-Norma-Control-Contract": "rule-controls-v1"}
    async with NormaClient(
        URL,
        agent_key="key",
        control_contract="rule-controls-v1",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload, headers=headers)),
    ) as client:
        listed = await list_agent_rules_typed(client, "hosted-test-agent")
        registered = await register_agent(client, Agent(agent_name="hosted-test-agent"))

    receipt = listed.control_resolutions[0]
    assert listed.control_contract == "rule-controls-v1"
    assert listed.agent_generation == "generation-1"
    assert listed.rules[0].id == 7
    assert receipt.effective_values == {"age": 120}
    assert isinstance(registered["control_resolutions"][0], ControlReceipt)
    assert registered["control_resolutions"][0].override_revision == 4
    with pytest.raises(TypeError, match="immutable"):
        receipt.effective_values["age"] = 180


async def test_public_agent_rule_list_rejects_corrupt_control_receipt() -> None:
    payload = _rule_readback_payload()
    receipts = payload["control_resolutions"]
    assert isinstance(receipts, list) and isinstance(receipts[0], dict)
    references = receipts[0]["references"]
    assert isinstance(references, list) and isinstance(references[0], dict)
    references[0]["key"] = 7
    async with NormaClient(
        URL,
        agent_key="key",
        control_contract="rule-controls-v1",
        transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json=payload, headers={"X-Norma-Control-Contract": "rule-controls-v1"}
        )),
    ) as client:
        with pytest.raises(ValidationError):
            await list_agent_rules_typed(client, "hosted-test-agent")


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/v1/check-runs/shadow", True),
        ("GET", "/api/v1/check-runs", True),
        ("GET", "/api/v1/check-runs/run-1", True),
        ("POST", "/api/v1/check-runs/run-1", False),
        ("GET", "/api/v1/operations/observe-metrics", False),
        ("POST", "/api/v1/operations/observe-metrics", False),
        ("GET", "/api/v1/agents/hosted-test-agent", False),
    ],
)
def test_contract_negotiation_route_allowlist(method: str, path: str, expected: bool) -> None:
    assert is_control_contract_route(method, path) is expected


async def test_opted_in_rule_refresh_sends_header_and_requires_both_acks() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"rules": [], "control_contract": "rule-controls-v1"},
            headers={"X-Norma-Control-Contract": "rule-controls-v1"},
        )

    async with NormaClient(
        URL,
        agent_key="key",
        control_contract="rule-controls-v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        data = await _list_agent_rules(client, "hosted-test-agent")

    assert data["rules"] == []
    assert len(seen) == 1
    assert seen[0].headers["X-Norma-Control-Contract"] == "rule-controls-v1"


async def test_opted_in_registration_sends_header_and_preserves_effective_rules() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"rules": [], "created": False, "control_contract": "rule-controls-v1"},
            headers={"X-Norma-Control-Contract": "rule-controls-v1"},
        )

    async with NormaClient(
        URL,
        agent_key="key",
        control_contract="rule-controls-v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        data = await _register_agent(client, Agent(agent_name="hosted-test-agent"))

    assert data["rules"] == []
    assert len(seen) == 1
    assert seen[0].url.path == "/api/runtime/api/v1/agents/register"
    assert seen[0].headers["X-Norma-Control-Contract"] == "rule-controls-v1"


async def test_client_rejects_manual_contract_header_on_unreviewed_route() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async with NormaClient(
        URL, agent_key="key", control_contract="rule-controls-v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="headers are managed"):
            await client._request_runtime_resource(
                "GET", "/api/v1/agents/hosted-test-agent",
                headers={"X-Norma-Control-Contract": "rule-controls-v1"},
            )
    assert seen == []


@pytest.mark.parametrize(
    "headers,payload",
    [
        ({"X-Norma-Control-Contract": "rule-controls-v1"}, {"rules": []}),
        ({}, {"rules": [], "control_contract": "rule-controls-v1"}),
        (
            {"X-Norma-Control-Contract": "rule-controls-v1"},
            {"rules": [], "control_contract": "other"},
        ),
    ],
)
async def test_rule_refresh_rejects_unproven_control_contract(
    headers: dict[str, str], payload: dict[str, object]
) -> None:
    async with NormaClient(
        URL,
        agent_key="key",
        control_contract="rule-controls-v1",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload, headers=headers)),
    ) as client:
        with pytest.raises(UnsupportedControlContractError):
            await _list_agent_rules(client, "hosted-test-agent")


def test_control_receipt_and_frozen_evidence_survive_public_decision_adapter() -> None:
    receipt = {
        "contract": "rule-controls-v1",
        "capabilities_fingerprint": "a" * 64,
        "declaration_fingerprint": "b" * 64,
        "defaults": {"age": 90},
        "effective_values": {"age": 120},
        "values_fingerprint": "c" * 64,
        "value_sources": {"age": "agent_override"},
        "references": [{
            "key": "age", "source_pointer": "/condition/evaluator/config/min",
            "destination_pointer": "/condition/evaluator/config/min",
            "evaluator_pointer": "/condition/evaluator", "slot": {},
        }],
        "overridden_keys": ["age"],
        "agent_generation": "generation-1",
        "agent_name": "hosted-test-agent",
        "rule_id": 7,
        "rule_version": 3,
        "override_revision": 4,
        "effective_fingerprint": "d" * 64,
    }
    evidence = {
        "schema_version": 1,
        "completeness": "incomplete",
        "agent_name": "hosted-test-agent",
        "agent_generation": "generation-1",
        "rule_id": 7,
        "rule_version": 3,
        "definition_fingerprint": "d" * 64,
        "receipt": receipt,
        "references": [{
            "key": "age", "destination_pointer": "/condition/evaluator/config/min",
            "configured_default": 90, "effective_value": 120,
            "value_source": "agent_override", "disposition": "evaluated",
            "numeric_comparisons": [],
        }],
    }
    response = CheckResponse.model_validate(
        {
            "execution_contract": "hosted-v1",
            "control_contract": "rule-controls-v1",
            "agent_generation": "generation-1",
            "control_resolutions": [receipt],
            "control_evidence": [evidence],
            "allowed": True,
            "confidence": 1.0,
        }
    )

    decision = decision_from_wire(response)

    assert decision.control_contract == "rule-controls-v1"
    assert decision.agent_generation == "generation-1"
    assert decision.control_resolutions[0].effective_values == {"age": 120}
    assert decision.control_evidence[0].receipt.value_sources == {"age": "agent_override"}
    assert decision.model_dump(mode="json")["control_evidence"][0]["completeness"] == "incomplete"


def test_check_event_forwards_control_disposition_without_private_values() -> None:
    receipt = {
        "contract": "rule-controls-v1", "capabilities_fingerprint": "a" * 64,
        "declaration_fingerprint": "b" * 64, "defaults": {"age": 18},
        "effective_values": {"age": 20}, "values_fingerprint": "c" * 64,
        "value_sources": {"age": "agent_override"},
        "references": [{
            "key": "age", "source_pointer": "/condition/evaluator/config/min",
            "destination_pointer": "/condition/evaluator/config/min",
            "evaluator_pointer": "/condition/evaluator", "slot": {},
        }],
        "overridden_keys": ["age"], "agent_generation": "generation-1",
        "agent_name": "hosted-test-agent", "rule_id": 7, "rule_version": 3,
        "override_revision": 1, "effective_fingerprint": "d" * 64,
    }
    response = CheckResponse.model_validate({
        "execution_contract": "hosted-v1", "control_contract": "rule-controls-v1",
        "agent_generation": "generation-1", "allowed": True, "confidence": 1.0,
        "matches": [{
            "rule_id": 7, "rule_name": "age-check", "action": "observe", "check_id": "check-7",
            "result": {"matched": True, "confidence": 1.0}}],
        "control_evidence": [{
            "schema_version": 1, "completeness": "incomplete", "agent_name": "hosted-test-agent",
            "agent_generation": "generation-1", "rule_id": 7, "rule_version": 3,
            "definition_fingerprint": "d" * 64, "receipt": receipt,
            "references": [{"key": "age", "destination_pointer": "/condition/evaluator/config/min",
                "configured_default": 18, "effective_value": 20,
                "value_source": "agent_override", "disposition": "evaluated",
                "numeric_comparisons": [{"field_path": "age", "operator": "min", "observed": 19,
                    "expected": 18, "outcome": "passed"}]}]}],
    })
    request = CheckRequest(
        agent_name="hosted-test-agent",
        step=Step(type="tool", name="read", input={}),
        stage="pre",
    )

    events = build_rule_check_events(response, request, {}, "1" * 32, "2" * 16, None)

    assert events[0].metadata["control_contract"] == "rule-controls-v1"
    evidence = events[0].metadata["control_evidence"]
    assert evidence[0]["references"][0]["disposition"] == "evaluated"
    assert "observed" not in str(evidence)
    assert "configured_default" not in str(events[0].metadata)
    assert "effective_value" not in str(events[0].metadata)


def test_control_receipts_are_strict_and_recursively_immutable() -> None:
    payload = {
        "contract": "rule-controls-v1", "capabilities_fingerprint": "a" * 64,
        "declaration_fingerprint": "b" * 64, "defaults": {"nested": {"items": ["one"]}},
        "effective_values": {"nested": {"items": ["two"]}}, "values_fingerprint": "c" * 64,
        "value_sources": {"nested": "default"}, "references": [], "overridden_keys": [],
        "agent_generation": "generation-1", "agent_name": "hosted-test-agent", "rule_id": 7,
        "rule_version": 3, "override_revision": 1, "effective_fingerprint": "d" * 64,
    }
    receipt = ControlReceipt.model_validate(payload)

    assert isinstance(receipt.defaults["nested"], dict)
    with pytest.raises(TypeError, match="immutable"):
        receipt.defaults["nested"]["items"].append("mutated")
    with pytest.raises(TypeError, match="immutable"):
        receipt.effective_values["nested"]["items"] += ["mutated"]
    with pytest.raises(ValidationError):
        ControlReceipt.model_validate({**payload, "rule_id": True})
    with pytest.raises(ValidationError):
        NumericComparisonEvidence.model_validate(
            {
                "field_path": "age",
                "operator": "min",
                "observed": True,
                "expected": 18,
                "outcome": "passed",
            }
        )


def test_incomplete_evidence_references_match_receipt_values_and_have_unique_identity() -> None:
    receipt_reference = {
        "key": "age", "source_pointer": "/condition/evaluator/config/min",
        "destination_pointer": "/condition/evaluator/config/min",
        "evaluator_pointer": "/condition/evaluator", "slot": {},
    }
    receipt = {
        "contract": "rule-controls-v1", "capabilities_fingerprint": "a" * 64,
        "declaration_fingerprint": "b" * 64, "defaults": {"age": 1},
        "effective_values": {"age": 2}, "values_fingerprint": "c" * 64,
        "value_sources": {"age": "agent_override"}, "references": [receipt_reference],
        "overridden_keys": ["age"], "agent_generation": "generation-1",
        "agent_name": "hosted-test-agent", "rule_id": 7, "rule_version": 3,
        "override_revision": 1, "effective_fingerprint": "d" * 64,
    }
    reference = {
        "key": "age", "destination_pointer": receipt_reference["destination_pointer"],
        "configured_default": 1, "effective_value": 2,
        "value_source": "agent_override", "disposition": "evaluated",
        "numeric_comparisons": [],
    }
    base = {
        "schema_version": 1, "completeness": "incomplete",
        "agent_name": "hosted-test-agent", "agent_generation": "generation-1",
        "rule_id": 7, "rule_version": 3, "definition_fingerprint": "d" * 64,
        "receipt": receipt, "references": [reference],
    }
    assert ControlEvidenceSnapshot.model_validate(base).references[0].effective_value == 2

    corruptions = [
        {**base, "receipt": None},
        {**base, "references": [reference, reference]},
        {**base, "references": [{**reference, "configured_default": True}]},
        {**base, "references": [{**reference, "configured_default": "1"}]},
        {**base, "references": [{**reference, "effective_value": False}]},
        {**base, "references": [{**reference, "value_source": "default"}]},
        {**base, "receipt": {**receipt, "defaults": {}}},
        {**base, "rule_id": True},
        {**base, "rule_id": 7.5},
        {**base, "rule_id": 0},
    ]
    for payload in corruptions:
        with pytest.raises(ValidationError):
            ControlEvidenceSnapshot.model_validate(payload)
