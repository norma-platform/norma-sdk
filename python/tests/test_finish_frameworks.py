# Modified for Norma.
"""Real framework-runner contracts for optional finish helpers."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from agents import Agent as OpenAIAgent
from agents import (
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    function_tool,
    output_guardrail,
)
from agents.testing.model import ScriptedModel, assistant_message, function_call
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import ModelMessage, ModelResponse, ModelRetry, RunContext, TextPart, ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from norma.client import NormaClient
from norma.finish import TurnRecorder
from norma.integrations.openai_agents_finish import run_openai_agents_with_finish
from norma.integrations.pydantic_ai_finish import PydanticFinishGuard
from norma.models import ContinuationState, FinishVerdict


def _verdict(kind: str, count: int, reason: str = "Please correct the claim") -> FinishVerdict:
    if kind == "hold":
        return FinishVerdict(
            execution_contract="hosted-v1",
            verdict="hold",
            reason=reason,
            reason_codes=["claim_contradicted"],
            held_by=[9],
            would_hold_by=[],
            continuation=ContinuationState(count=count, max=2),
            allow_finish=False,
            outcome="held",
        )
    return FinishVerdict(
        execution_contract="hosted-v1",
        verdict="pass",
        reason="Finish accepted",
        reason_codes=[],
        held_by=[],
        would_hold_by=[],
        continuation=ContinuationState(count=count, max=2),
        allow_finish=True,
        outcome="completed",
    )


def _client(seen: list[dict[str, object]]) -> NormaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        turn = body["step"]["input"]
        seen.append(turn)
        count = turn["continuation"]["count"]
        kind = "hold" if count == 0 else "pass"
        return httpx.Response(200, json=_verdict(kind, count).model_dump(mode="json"))

    return NormaClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_pydantic_ai_modelretry_keeps_completed_tool_history() -> None:
    tool_calls = 0
    model_inputs: list[list[ModelMessage]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _ = info
        model_inputs.append(messages)
        if len(model_inputs) == 1:
            return ModelResponse(parts=[ToolCallPart("record", {})])
        if len(model_inputs) == 2:
            return ModelResponse(parts=[TextPart("All tests pass")])
        return ModelResponse(parts=[TextPart("The tests did not pass")])

    agent = PydanticAgent(FunctionModel(model))
    recorder = TurnRecorder(session_id="pydantic-session", turn_id="turn")

    @agent.tool_plain
    def record() -> str:
        nonlocal tool_calls
        tool_calls += 1
        recorder.record_action(
            kind="shell",
            native_tool="record",
            command="pytest",
            status="failed",
            exit_code=1,
            output_tail="1 failed",
        )
        return "failed tests: 1"

    seen: list[dict[str, object]] = []
    async with _client(seen) as client:
        guard = PydanticFinishGuard(agent)
        result = await guard.run("Run tests", recorder, client=client, agent_name="test-agent")

    assert result.result.output == "The tests did not pass"
    assert result.verdict.verdict == "pass"
    assert [turn["continuation"]["count"] for turn in seen] == [0, 1]
    assert tool_calls == 1
    assert all(turn["actions"][0]["status"] == "failed" for turn in seen)
    assert all(turn["actions"][0]["command"] == "pytest" for turn in seen)
    assert any(
        isinstance(part, ToolReturnPart)
        for message in model_inputs[-1]
        if hasattr(message, "parts")
        for part in message.parts
    )


@pytest.mark.asyncio
async def test_openai_agents_guardrail_resumes_without_replaying_tool() -> None:
    tool_calls = 0

    recorder = TurnRecorder(session_id="openai-session", turn_id="turn")

    @function_tool
    def record() -> str:
        """Record the result of the test action."""
        nonlocal tool_calls
        tool_calls += 1
        recorder.record_action(
            kind="shell",
            native_tool="record",
            command="pytest",
            status="failed",
            exit_code=1,
            output_tail="1 failed",
        )
        return "failed tests: 1"

    scripted = ScriptedModel(
        [
            [function_call("record", {}, call_id="call-1")],
            [assistant_message("All tests pass", item_id="claim-1")],
            [assistant_message("The tests did not pass", item_id="claim-2")],
        ]
    )
    agent = OpenAIAgent(name="test-agent", model=scripted, tools=[record])
    seen: list[dict[str, object]] = []
    async with _client(seen) as client:
        result = await run_openai_agents_with_finish(
            agent, "Run tests", recorder, client=client, agent_name="test-agent"
        )

    assert result.result.final_output == "The tests did not pass"
    assert result.verdict.verdict == "pass"
    assert [turn["continuation"]["count"] for turn in seen] == [0, 1]
    assert tool_calls == 1
    assert all(turn["actions"][0]["status"] == "failed" for turn in seen)
    assert all(turn["actions"][0]["command"] == "pytest" for turn in seen)
    assert "Please correct the claim" in str(scripted.calls[-1].input)
    assert "call-1" in str(scripted.calls[-1].input)


@pytest.mark.asyncio
async def test_pydantic_ai_same_agent_concurrent_turns_are_isolated() -> None:
    agent = PydanticAgent(
        FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("Done")]))
    )
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        turn = json.loads(request.read())["step"]["input"]
        seen.append(turn)
        return httpx.Response(200, json=_verdict("pass", 0).model_dump(mode="json"))

    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        guard = PydanticFinishGuard(agent)
        assert PydanticFinishGuard(agent) is guard
        first, second = await asyncio.gather(
            guard.run(
                "first",
                TurnRecorder(session_id="one", turn_id="turn"),
                client=client,
                agent_name="test-agent",
            ),
            guard.run(
                "second",
                TurnRecorder(session_id="two", turn_id="turn"),
                client=client,
                agent_name="test-agent",
            ),
        )

    assert first.verdict.verdict == second.verdict.verdict == "pass"
    assert {turn["session_id"] for turn in seen} == {"one", "two"}
    assert all(turn["continuation"]["count"] == 0 for turn in seen)


@pytest.mark.asyncio
async def test_pydantic_other_validator_retry_does_not_consume_norma_count() -> None:
    model_calls = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        return ModelResponse(parts=[TextPart("short" if model_calls == 1 else "Accepted")])

    agent = PydanticAgent(FunctionModel(model))

    @agent.output_validator
    def other_validator(ctx: RunContext[object], output: str) -> str:
        if output == "short":
            raise ModelRetry("Output too short")
        return output

    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        turn = json.loads(request.read())["step"]["input"]
        seen.append(turn)
        return httpx.Response(200, json=_verdict("pass", 0).model_dump(mode="json"))

    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        result = await PydanticFinishGuard(agent).run(
            "work",
            TurnRecorder(session_id="other-retry", turn_id="turn"),
            client=client,
            agent_name="test-agent",
        )
    assert result.result.output == "Accepted"
    assert len(seen) == 1
    assert seen[0]["continuation"]["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 2, 3])
async def test_pydantic_native_retry_stops_at_configured_cap(limit: int) -> None:
    agent = PydanticAgent(
        FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("Uncorrected claim")]))
    )
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        turn = json.loads(request.read())["step"]["input"]
        count = turn["continuation"]["count"]
        seen.append(count)
        if count < limit:
            verdict = FinishVerdict(
                execution_contract="hosted-v1",
                verdict="hold",
                reason="Correct this claim",
                reason_codes=["claim_contradicted"],
                held_by=[9],
                would_hold_by=[],
                continuation=ContinuationState(count=count, max=limit),
                allow_finish=False,
                outcome="held",
            )
        else:
            verdict = FinishVerdict(
                execution_contract="hosted-v1",
                verdict="unsure",
                reason="Finished unverified at continuation cap",
                reason_codes=["unsure"],
                held_by=[],
                would_hold_by=[9],
                continuation=ContinuationState(count=count, max=limit),
                allow_finish=True,
                outcome="finished_unverified",
            )
        return httpx.Response(200, json=verdict.model_dump(mode="json"))

    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        result = await PydanticFinishGuard(agent).run(
            "work",
            TurnRecorder(session_id=f"cap-{limit}", turn_id="turn", max_continuations=limit),
            client=client,
            agent_name="test-agent",
        )
    assert result.verdict.verdict == "unsure"
    assert seen == list(range(limit + 1))


@pytest.mark.asyncio
async def test_openai_other_output_guardrail_tripwire_propagates() -> None:
    @output_guardrail(name="user_guardrail")
    async def user_guardrail(ctx, agent, output) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    agent = OpenAIAgent(
        name="test-agent",
        model=ScriptedModel([[assistant_message("Done")]]),
        output_guardrails=[user_guardrail],
    )
    recorder = TurnRecorder(session_id="user-tripwire", turn_id="turn")
    with pytest.raises(OutputGuardrailTripwireTriggered) as error:
        await run_openai_agents_with_finish(agent, "Work", recorder)
    assert error.value.guardrail_result.guardrail is user_guardrail


@pytest.mark.asyncio
async def test_openai_hold_then_handoff_reports_unverified_final_agent() -> None:
    scripted = ScriptedModel(
        [
            [assistant_message("All tests pass", item_id="claim")],
            [function_call("transfer_to_worker", {}, call_id="handoff-1")],
            [assistant_message("The tests did not pass", item_id="worker-final")],
        ]
    )
    worker = OpenAIAgent(name="worker", model=scripted)
    agent = OpenAIAgent(name="test-agent", model=scripted, handoffs=[worker])
    seen: list[dict[str, object]] = []
    async with _client(seen) as client:
        result = await run_openai_agents_with_finish(
            agent,
            "Work",
            TurnRecorder(session_id="handoff", turn_id="turn"),
            client=client,
            agent_name="test-agent",
        )
    assert result.result.final_output == "The tests did not pass"
    assert result.verdict.verdict == "unavailable"
    assert "final agent" in result.verdict.reason
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 2, 3])
async def test_openai_guardrail_stops_at_configured_cap(limit: int) -> None:
    scripted = ScriptedModel(
        [
            [assistant_message("Uncorrected claim", item_id=f"claim-{index}")]
            for index in range(limit + 1)
        ]
    )
    agent = OpenAIAgent(name="test-agent", model=scripted)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        turn = json.loads(request.read())["step"]["input"]
        count = turn["continuation"]["count"]
        seen.append(count)
        if count < limit:
            verdict = FinishVerdict(
                execution_contract="hosted-v1",
                verdict="hold",
                reason="Correct this claim",
                reason_codes=["claim_contradicted"],
                held_by=[9],
                would_hold_by=[],
                continuation=ContinuationState(count=count, max=limit),
                allow_finish=False,
                outcome="held",
            )
        else:
            verdict = FinishVerdict(
                execution_contract="hosted-v1",
                verdict="unsure",
                reason="Finished unverified at continuation cap",
                reason_codes=["unsure"],
                held_by=[],
                would_hold_by=[9],
                continuation=ContinuationState(count=count, max=limit),
                allow_finish=True,
                outcome="finished_unverified",
            )
        return httpx.Response(200, json=verdict.model_dump(mode="json"))

    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_openai_agents_with_finish(
            agent,
            "Work",
            TurnRecorder(session_id=f"openai-cap-{limit}", turn_id="turn", max_continuations=limit),
            client=client,
            agent_name="test-agent",
        )
    assert result.verdict.verdict == "unsure"
    assert seen == list(range(limit + 1))


@pytest.mark.asyncio
async def test_both_framework_helpers_allow_finish_on_runtime_outage() -> None:
    def outage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with NormaClient(transport=httpx.MockTransport(outage)) as client:
        pydantic_agent = PydanticAgent(
            FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("Done")]))
        )
        pydantic_result = await PydanticFinishGuard(pydantic_agent).run(
            "Work",
            TurnRecorder(session_id="outage-pydantic", turn_id="turn"),
            client=client,
            agent_name="test-agent",
        )
        openai_agent = OpenAIAgent(
            name="test-agent", model=ScriptedModel([[assistant_message("Done")]])
        )
        openai_result = await run_openai_agents_with_finish(
            openai_agent,
            "Work",
            TurnRecorder(session_id="outage-openai", turn_id="turn"),
            client=client,
            agent_name="test-agent",
        )
    assert pydantic_result.verdict.verdict == "unavailable"
    assert openai_result.verdict.verdict == "unavailable"
    assert pydantic_result.verdict.allow_finish and openai_result.verdict.allow_finish
