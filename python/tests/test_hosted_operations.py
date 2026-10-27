# Modified for Norma.
"""Operation creation requires hosted admission; historical receipts stay readable."""

import json
from typing import Any

import httpx
import pytest

from norma import NormaClient, operations
from norma.models import CreateOperationRequest, Step


def _receipt(*, hosted: bool) -> dict[str, Any]:
    check: dict[str, Any] = {"allowed": True, "confidence": 1}
    if hosted:
        check["execution_contract"] = "hosted-v1"
    return {
        "id": "operation-1",
        "operation_key": "key-1",
        "agent_name": "hosted-agent",
        "created_at": "2026-10-19T00:00:00Z",
        "request_fingerprint": "fingerprint",
        "request": {
            "agent_name": "hosted-agent",
            "operation_key": "key-1",
            "step": {"type": "tool", "name": "write", "input": {}},
            "error_policy": "block" if hosted else "allow",
            "execution_mode": "hosted" if hosted else None,
        },
        "decision": {"outcome": "allow", "reason": "checked", "check": check},
    }


@pytest.mark.parametrize("hosted", [True, False])
async def test_creation_negotiates_and_requires_hosted_ack(hosted: bool) -> None:
    # Given: a real HTTP boundary returning either an acknowledged or legacy receipt.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_receipt(hosted=hosted))

    proposal = CreateOperationRequest(
        agent_name="hosted-agent",
        operation_key="key-1",
        step=Step(type="tool", name="write", input={}),
    )
    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        # When: creating an operation through the public SDK helper.
        if hosted:
            record = await operations.create(client, proposal)
            assert record.decision.execution_contract == "hosted-v1"
        else:
            with pytest.raises(RuntimeError, match="execution_contract"):
                await operations.create(client, proposal)
    # Then: the request opts in, and an old server cannot silently satisfy it.
    assert json.loads(seen[0].content)["execution_mode"] == "hosted"


async def test_historical_receipts_are_read_without_claiming_hosted_admission() -> None:
    # Given: a stored legacy operation with a permissive error policy.
    async with NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=_receipt(hosted=False)),
        )
    ) as client:
        # When: inspecting it through the historical read API.
        record = await operations.get(client, "operation-1", agent_name="hosted-agent")
    # Then: history is readable and does not gain a hosted contract or a new policy.
    assert record.decision.execution_contract is None
    assert record.request.execution_mode is None
    assert record.request.error_policy == "allow"


async def test_opted_in_operation_read_preserves_frozen_control_receipt() -> None:
    receipt = _receipt(hosted=True)
    receipt["control_contract"] = "rule-controls-v1"
    receipt["agent_generation"] = "generation-1"
    receipt["decision"]["check"].update(
        {
            "control_contract": "rule-controls-v1",
            "agent_generation": "generation-1",
            "control_resolutions": [
                {
                    "contract": "rule-controls-v1",
                    "capabilities_fingerprint": "a" * 64,
                    "declaration_fingerprint": "b" * 64,
                    "defaults": {"age": 90},
                    "effective_values": {"age": 120},
                    "values_fingerprint": "c" * 64,
                    "value_sources": {"age": "agent_override"},
                    "references": [],
                    "overridden_keys": ["age"],
                    "agent_generation": "generation-1",
                    "agent_name": "hosted-agent",
                    "rule_id": 7,
                    "rule_version": 3,
                    "override_revision": 4,
                    "effective_fingerprint": "d" * 64,
                }
            ],
        }
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=receipt,
            headers={"X-Norma-Control-Contract": "rule-controls-v1"},
        )

    async with NormaClient(
        control_contract="rule-controls-v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        record = await operations.get(client, "operation-1", agent_name="hosted-agent")

    assert len(seen) == 1
    assert seen[0].headers["X-Norma-Control-Contract"] == "rule-controls-v1"
    assert record.agent_generation == "generation-1"
    assert record.decision.control_resolutions[0].effective_values == {"age": 120}


@pytest.mark.parametrize("mismatch", ["agent", "proposal", "key", "target"])
async def test_creation_rejects_unrelated_hosted_receipt(mismatch: str) -> None:
    receipt = _receipt(hosted=True)
    if mismatch == "agent":
        receipt["agent_name"] = "other-agent"
    elif mismatch == "proposal":
        receipt["request"]["step"]["input"] = {"different": True}
    elif mismatch == "key":
        receipt["operation_key"] = "other-key"
    else:
        receipt["target_type"], receipt["target_id"] = "document", "other"
    proposal = CreateOperationRequest(
        agent_name="hosted-agent",
        operation_key="key-1",
        step=Step(type="tool", name="write", input={}),
    )
    async with NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=receipt),
        )
    ) as client:
        with pytest.raises(RuntimeError, match="receipt"):
            await operations.create(client, proposal)


async def test_historical_local_snapshot_is_readable_without_authoring_support() -> None:
    receipt = _receipt(hosted=False)
    receipt["rules"] = [
        {
            "rule_id": 7,
            "name": "old-local",
            "definition": {"execution": "sdk"},
            "definition_fingerprint": "old",
            "disposition": "skipped",
        }
    ]
    async with NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=receipt),
        )
    ) as client:
        record = await operations.get(client, "operation-1", agent_name="hosted-agent")
    assert record.rules[0].rule_id == 7
    assert record.rules[0].status == "unsupported"
    assert record.rules[0].definition is None
    assert record.model_dump()["rules"][0]["rule_id"] == 7
    assert "check" not in record.decision.model_dump()


@pytest.mark.parametrize("advisory", [True, False])
async def test_operation_allow_requires_explainable_runtime_decision(advisory: bool) -> None:
    receipt = _receipt(hosted=True)
    receipt["decision"]["check"]["allowed"] = False
    if advisory:
        receipt["decision"]["check"]["matches"] = [
            {
                "rule_id": 1,
                "rule_name": "advisory",
                "action": "observe",
                "result": {"matched": True, "confidence": 1},
            }
        ]
    async with NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=receipt),
        )
    ) as client:
        if advisory:
            record = await operations.get(client, "operation-1", agent_name="hosted-agent")
            assert record.decision.allowed
            assert record.decision.outcome == "allow"
        else:
            with pytest.raises(RuntimeError, match="unexplained blocked"):
                await operations.get(client, "operation-1", agent_name="hosted-agent")


async def test_read_and_list_reject_cross_agent_receipts() -> None:
    receipt = _receipt(hosted=True)
    async with NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=receipt),
        )
    ) as client:
        with pytest.raises(RuntimeError, match="identity"):
            await operations.get(client, "operation-1", agent_name="different-agent")
    async with NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"operations": [receipt]}),
        )
    ) as client:
        with pytest.raises(RuntimeError, match="identity"):
            await operations.list_for_agent(client, "different-agent")


async def test_retained_semantic_rule_definition_is_normalized() -> None:
    receipt = _receipt(hosted=True)
    receipt["rules"] = [
        {
            "rule_id": 7,
            "name": "semantic",
            "definition": {
                "execution": "server",
                "action": {"decision": "observe"},
                "condition": {
                    "evaluator": {
                        "name": "semantic",
                        "config": {
                            "questions": [{"id": "claim", "type": "statement"}],
                            "decision": {"rules": [{"question_id": "claim", "type": "statement"}]},
                            "examples": [
                                {
                                    "expected_answers": {
                                        "claim": {"type": "statement", "support_score": 0.7}
                                    }
                                }
                            ],
                        },
                    }
                },
            },
            "definition_fingerprint": "definition",
            "disposition": "evaluated",
        }
    ]
    async with NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=receipt),
        )
    ) as client:
        record = await operations.get(client, "operation-1", agent_name="hosted-agent")
    data = record.model_dump(mode="json")
    assert "support_score" in json.dumps(data)
    assert data["rules"][0]["status"] == "hosted"
    assert data["rules"][0]["rule_id"] == 7


async def test_malformed_advisory_nonmatch_cannot_explain_allow() -> None:
    receipt = _receipt(hosted=True)
    receipt["decision"]["check"].update(
        {
            "allowed": False,
            "matches": [
                {
                    "rule_id": 1,
                    "rule_name": "advisory",
                    "action": "observe",
                    "result": {"matched": False, "confidence": 1},
                }
            ],
        }
    )
    async with NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=receipt),
        )
    ) as client:
        with pytest.raises(RuntimeError, match="unexplained blocked"):
            await operations.get(client, "operation-1", agent_name="hosted-agent")


@pytest.mark.parametrize("mode,mixed,allowed", [
    ("observe", False, True), ("enforce", False, False),
    (None, False, False), ("observe", True, False),
])
async def test_observed_unresolved_receipt_respects_enforcing_policy_only(mode, mixed, allowed):
    receipt = _receipt(hosted=True)
    receipt["request"]["unresolved_policy"] = "block"
    match = {"rule_id": 1, "rule_name": "uncertain", "action": "observe",
             "effective_action": "observe", "result": {
                 "matched": False, "confidence": 0, "outcome": "unresolved",
                 "unresolved_action": "observe"}}
    if mode is not None:
        match["mode"] = mode
    receipt["decision"]["check"]["unresolved"] = [match]
    if mixed:
        receipt["decision"]["check"]["unresolved"].append({
            **match, "rule_id": 2, "mode": "enforce",
        })
    async with NormaClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=receipt),
    )) as client:
        if allowed:
            result = await operations.get(client, "operation-1", agent_name="hosted-agent")
            assert result.decision.allowed
            assert result.decision.checks[0].mode == "observe"
        else:
            with pytest.raises(RuntimeError, match="retained unresolved policy"):
                await operations.get(client, "operation-1", agent_name="hosted-agent")
