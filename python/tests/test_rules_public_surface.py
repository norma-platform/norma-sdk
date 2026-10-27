# Modified for Norma.
"""Public Rules contracts, with explicit wire-boundary compatibility evidence."""

import importlib
import inspect

import httpx
import pytest
from pydantic import ValidationError

import norma
from norma import models
from norma.models import Decision, RuleAction, RuleDefinition, RuleMatch, RuleScope, Step


def test_public_exports_use_typed_native_rules() -> None:
    assert models.RuleMatch is RuleMatch
    assert models.Decision is Decision
    assert norma.RuleViolationError
    assert "rules_file" not in inspect.signature(norma.init).parameters
    assert hasattr(norma, "refresh_rules")
    for module in ("norma.local_engine", "norma.local_evaluator"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_hosted_rule_schema_and_local_authoring_rejection() -> None:
    definition = RuleDefinition(
        condition={"evaluator": {"name": "json", "config": {}}},
        action=RuleAction(decision="deny"),
        scope=RuleScope(step_types=["custom_checkpoint"]),
    )
    assert definition.execution == "server"
    with pytest.raises(ValidationError):
        RuleDefinition.model_validate({**definition.model_dump(), "execution": "sdk"})
    for retired in ("template", "template_values", "parameters"):
        assert retired not in RuleDefinition.model_fields
        with pytest.raises(ValidationError):
            RuleDefinition.model_validate({**definition.model_dump(), retired: {}})
    assert "rendered_state" not in inspect.signature(norma.agents.list_agent_rules_typed).parameters
    assert RuleDefinition.model_json_schema()["properties"]["execution"]["const"] == "server"


def test_rule_error_constructor_and_event_schema_use_public_identifiers() -> None:
    error = norma.RuleViolationError(rule_id=7, rule_name="bounded", check_id="check-7")
    assert (error.rule_id, error.rule_name, error.check_id) == (7, "bounded", "check-7")
    with pytest.raises(TypeError):
        norma.RuleViolationError(unrecognized_id=7)  # type: ignore[call-arg]
    props = models.RuleCheckEvent.model_json_schema()["properties"]
    assert {"rule_id", "rule_name", "check_id"} <= props.keys()


@pytest.mark.parametrize("status", ["error", "unresolved", "matched"])
def test_decision_cannot_allow_blocking_check(status: str) -> None:
    check = RuleMatch.model_validate(
        {
            "rule_id": 7,
            "rule_name": "bounded",
            "check_id": "check-7",
            "status": status,
            "action": "deny",
            "result": {"matched": status == "matched", "confidence": 1},
        }
    )
    result = Decision(allowed=True, checks=[check])
    assert not result.allowed
    serialized = result.model_dump()
    assert serialized["checks"][0]["rule_id"] == 7
    assert serialized["checks"][0]["check_id"] == "check-7"


async def test_real_transport_normalizes_decision_without_rewriting_user_metadata() -> None:
    payload = {
        "allowed": False,
        "confidence": 1,
        "execution_contract": "hosted-v1",
        "matches": [
            {
                "rule_id": 7,
                "rule_name": "bounded",
                "check_id": "check-7",
                "action": "observe",
                "result": {
                    "matched": True,
                    "confidence": 1,
                    "metadata": {"rule_id": "user-owned-value"},
                },
            }
        ],
    }
    async with norma.NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload),
        )
    ) as client:
        result = await norma.checks.check(
            client, "public-agent", Step(type="tool", name="read", input={}), "pre"
        )
    assert isinstance(result, Decision)
    assert not result.allowed  # Explicit runtime denial wins even with advisory matches.
    assert result.checks[0].rule_id == 7
    assert result.checks[0].result.metadata == {"rule_id": "user-owned-value"}
    assert set(result.model_dump()) == {
        "allowed",
        "reason",
        "checks",
        "execution_contract",
        "recovery_feedback",
    }


async def test_uninitialized_sync_and_async_checkpoints_never_execute() -> None:
    effects = []

    @norma.checkpoint()
    def sync_action() -> None:
        effects.append("sync")

    @norma.checkpoint()
    async def async_action() -> None:
        effects.append("async")

    with pytest.raises(norma.CheckError, match="not initialized"):
        sync_action()
    with pytest.raises(norma.CheckError, match="not initialized"):
        await async_action()
    assert effects == []


async def test_nested_semantic_answers_and_feedback_are_public() -> None:
    observation = {
        "status": "completed",
        "answers": {"claim": {"type": "statement", "support_score": 0.75}},
        "evidence": {"fingerprint": "e", "complete": True},
        "definition_fingerprint": "d",
        "question_fingerprint": "q",
        "evidence_fingerprint": "e",
        "check_release": "release",
    }
    payload = {
        "allowed": False,
        "confidence": 1,
        "execution_contract": "hosted-v1",
        "matches": [
            {
                "rule_id": 7,
                "rule_name": "bounded",
                "action": "deny",
                "result": {"matched": True, "confidence": 1, "semantic_observation": observation},
            }
        ],
        "recovery_feedback": [
            {
                "rule_id": 7,
                "check_id": "check-7",
                "outcome": "matched",
                "reason": "criterion_triggered",
            }
        ],
    }
    async with norma.NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload),
        )
    ) as client:
        result = await norma.checks.check(
            client, "public-agent", Step(type="tool", name="read", input={}), "pre"
        )
    data = result.model_dump(mode="json")
    answer = data["checks"][0]["result"]["semantic_observation"]["answers"]["claim"]
    assert answer == {"type": "statement", "support_score": 0.75}
    assert data["recovery_feedback"][0]["rule_id"] == 7
    assert data["recovery_feedback"][0]["check_id"] == "check-7"
    assert answer["support_score"] == 0.75


async def test_agent_summaries_and_overwrite_details_normalize_known_fields() -> None:
    responses = [
        {"agents": [{"agent_name": "public-agent", "active_rules_count": 2}]},
        {
            "rules": [
                {"id": 7, "name": "old", "rule": {"execution": "sdk"}},
                {"id": 8, "name": "draft", "rule": {"template": {}, "enabled": False}},
            ],
            "overwrite_changes": {
                "evaluator_removals": [
                    {
                        "rule_ids": [7],
                        "rule_names": ["old"],
                        "referenced_by_active_rules": True,
                    }
                ]
            },
            "agent_metadata": {"rule_id": "caller-owned"},
        },
    ]
    async with norma.NormaClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=responses.pop(0)),
        )
    ) as client:
        summary = await norma.agents.list_agents(client)
        registered = await norma.agents.register_agent(
            client, models.Agent(agent_name="public-agent")
        )
    assert summary["agents"][0]["active_rules_count"] == 2
    assert registered["rules"][0]["definition"] is None
    assert registered["rules"][0]["status"] == "unsupported"
    assert registered["rules"][1]["status"] == "unsupported"
    assert registered["rules"][1]["definition"] is None
    removal = registered["overwrite_changes"]["evaluator_removals"][0]
    assert removal == {"rule_ids": [7], "rule_names": ["old"], "referenced_by_active_rules": True}
    assert registered["agent_metadata"] == {"rule_id": "caller-owned"}


@pytest.mark.parametrize("status", ["matched", "unresolved", "error"])
def test_observe_mode_retains_results_without_blocking(status):
    original = {"rule_id": 1, "rule_name": "observed", "status": status,
                "mode": "observe", "action": "steer",
                "steering_context": {"message": "Preserve guidance"},
                "result": {"matched": status == "matched", "confidence": 1,
                           "error": "synthetic error" if status == "error" else None}}
    from norma.checkpoints import _handle_checks_result

    bucket = {"matched": "matches", "unresolved": "unresolved", "error": "errors"}[status]
    _handle_checks_result({"allowed": True, bucket: [original]}, stage="pre")
    with pytest.raises(norma.RuleViolationError):
        _handle_checks_result({"allowed": False, bucket: [original]}, stage="pre")
    check = RuleMatch.model_validate(original)
    assert Decision(allowed=True, checks=[check]).allowed
    assert check.action == "steer"
    assert check.steering_context.message == "Preserve guidance"
    check.mode = "enforce"
    assert not Decision(allowed=True, checks=[check]).allowed
