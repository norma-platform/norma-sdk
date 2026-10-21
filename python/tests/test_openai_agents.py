# Modified for Norma.
"""Actual official Agents Runner with local scripted models and a mocked hosted boundary."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from agents import Agent, Model, ModelResponse, RunConfig, RunHooks, Runner, function_tool
from agents.exceptions import (
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailTripwireTriggered,
)
from agents.tool_context import ToolContext
from agents.tool_guardrails import ToolGuardrailFunctionOutput, ToolInputGuardrail
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from norma import NormaClient
from norma.integrations.openai_agents import AgentBinding, ToolCheckRecord, protect_function_tool

CONFIG = RunConfig(tracing_disabled=True)


class ScriptedModel(Model):
    def __init__(self, calls: list[tuple[str, str, str]]) -> None:
        self.calls = calls
        self.count = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.count += 1
        if self.count == 1:
            output = [
                ResponseFunctionToolCall(
                    type="function_call", name=name, arguments=raw, call_id=call_id
                )
                for name, raw, call_id in self.calls
            ]
        else:
            output = [
                ResponseOutputMessage(
                    id="message",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(type="output_text", text="Done locally", annotations=[])
                    ],
                )
            ]
        return ModelResponse(output=output, usage=Usage(), response_id=f"response-{self.count}")

    async def stream_response(self, *args: Any, **kwargs: Any):
        result = await self.get_response(*args, **kwargs)
        yield ResponseCompletedEvent(
            type="response.completed",
            sequence_number=self.count,
            response=Response(
                id=result.response_id,
                created_at=0,
                model="local",
                object="response",
                output=result.output,
                parallel_tool_calls=True,
                tool_choice="auto",
                tools=[],
                status="completed",
            ),
        )


class Boundary:
    def __init__(self, mode: str = "allow") -> None:
        self.mode = mode
        self.requests: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            name = request.url.path.rsplit("/", 1)[-1]
            if self.mode == "init_failure":
                return httpx.Response(403, json={"detail": "no"})
            return httpx.Response(200, json={"agent": {"agent_name": name}, "steps": []})
        data = json.loads(request.content)
        self.requests.append(data)
        if self.mode == "pause":
            self.entered.set()
            await self.release.wait()
        if self.mode == "http_503":
            return httpx.Response(503, json={"detail": "Unavailable"})
        if self.mode == "outage":
            raise httpx.ConnectError("offline", request=request)
        payload: dict[str, Any] = {
            "allowed": True,
            "confidence": 1,
            "execution_contract": "hosted-v1",
        }
        mode = self.mode
        if mode == "by_argument":
            mode = "deny" if data["step"]["input"]["text"] == "BLOCK" else "allow"
        if mode.startswith("post_"):
            mode = mode[5:] if data["stage"] == "post" else "allow"
        if mode in ("deny", "steer", "embedded_error", "unresolved"):
            action = "deny" if mode == "deny" else "steer"
            check = {
                "rule_id": 7,
                "rule_name": "bounded",
                "check_id": "check-7",
                "action": action,
                "steering_context": {"message": "Revise explicitly"},
                "result": {"matched": True, "confidence": 1},
            }
            payload.update(allowed=False, matches=[check])
            if mode == "unresolved":
                payload.pop("matches")
                payload["unresolved"] = [{**check, "action": "observe", "effective_action": "deny"}]
            if mode == "embedded_error":
                payload["non_matches"] = [
                    {
                        **check,
                        "action": "observe",
                        "result": {"matched": False, "confidence": 0, "error": "failed"},
                    }
                ]
        if mode == "missing_proof":
            payload.pop("execution_contract")
        if mode == "malformed":
            payload["allowed"] = "true"
        return httpx.Response(200, json=payload)


def make_tool(effects: list[str]):
    @function_tool(failure_error_function=None)
    async def record(text: str) -> str:
        """Record a deterministic in-memory effect."""
        effects.append(text)
        return text

    return record


async def setup(boundary: Boundary, client: NormaClient, effects: list[str], **options: Any):
    agent = Agent(name="trusted", model=ScriptedModel([("record", '{"text":"safe"}', "call-1")]))
    binding = AgentBinding(agent=agent, client=client, agent_name="trusted-agent")
    await binding.initialize()
    original = make_tool(effects)
    guarded = protect_function_tool(original, bindings=[binding], **options)
    agent.tools = [guarded]
    return agent, binding, original, guarded


@pytest.mark.asyncio
async def test_real_runner_allow_and_records() -> None:
    effects: list[str] = []
    boundary = Boundary()
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, original, guarded = await setup(boundary, client, effects)
        result = await Runner.run(agent, "Run", run_config=CONFIG)
        assert effects == ["safe"]
        assert original.tool_input_guardrails is None
        assert guarded.params_json_schema == original.params_json_schema
        record = result.tool_input_guardrail_results[0].output.output_info
        assert isinstance(record, ToolCheckRecord) and record.decision.allowed
        assert record.agent_name == "trusted-agent" and record.tool_call_id == "call-1"
        assert boundary.requests[0]["step"]["context"]["norma"]["tool_call_id"] == "call-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["deny", "outage", "http_503", "missing_proof", "malformed", "embedded_error", "unresolved"],
)
async def test_real_runner_failure_has_zero_effects(mode: str) -> None:
    boundary = Boundary(mode)
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects)
        with pytest.raises(ToolInputGuardrailTripwireTriggered) as failure:
            await Runner.run(agent, "Run", run_config=CONFIG)
        assert effects == []
        assert isinstance(failure.value.output.output_info, ToolCheckRecord)
        if mode == "http_503":
            assert failure.value.output.output_info.http_status == 503


@pytest.mark.asyncio
async def test_real_runner_steer_skips_call_and_retains_feedback() -> None:
    boundary = Boundary("steer")
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects)
        result = await Runner.run(agent, "Run", run_config=CONFIG)
        assert effects == []
        assert "Revise explicitly" in str(result.to_input_list())
        record = result.tool_input_guardrail_results[0].output.output_info
        assert record.decision.checks[0].rule_id == 7
        assert record.decision.checks[0].check_id == "check-7"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["uninitialized", "init_failure"])
async def test_initialization_failure_blocks(mode: str) -> None:
    boundary = Boundary(mode)
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent = Agent(
            name="trusted", model=ScriptedModel([("record", '{"text":"safe"}', "call-1")])
        )
        binding = AgentBinding(agent=agent, client=client, agent_name="trusted-agent")
        if mode == "init_failure":
            with pytest.raises(httpx.HTTPStatusError):
                await binding.initialize()
        agent.tools = [protect_function_tool(make_tool(effects), bindings=[binding])]
        with pytest.raises(ToolInputGuardrailTripwireTriggered):
            await Runner.run(agent, "Run", run_config=CONFIG)
        assert effects == [] and boundary.requests == []


@pytest.mark.asyncio
async def test_active_agent_identity_not_name() -> None:
    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, guarded = await setup(boundary, client, effects)
        impostor = Agent(name=agent.name, model=agent.model, tools=[guarded])
        with pytest.raises(ToolInputGuardrailTripwireTriggered):
            await Runner.run(impostor, "Run", run_config=CONFIG)
        assert effects == [] and boundary.requests == []


@pytest.mark.asyncio
async def test_postcheck_cannot_undo_effect() -> None:
    boundary = Boundary("post_deny")
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects, check_output=True)
        with pytest.raises(ToolOutputGuardrailTripwireTriggered):
            await Runner.run(agent, "Run", run_config=CONFIG)
        assert effects == ["safe"]
        assert [item["stage"] for item in boundary.requests] == ["pre", "post"]


@pytest.mark.asyncio
async def test_argument_mutation_after_guard_does_not_execute() -> None:
    class MutatingHooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            context.tool_arguments = '{"text":"changed"}'

    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects)
        with pytest.raises(ToolInputGuardrailTripwireTriggered):
            await Runner.run(agent, "Run", run_config=CONFIG, hooks=MutatingHooks())
        assert effects == []


@pytest.mark.asyncio
async def test_existing_guardrail_composition() -> None:
    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, binding, original, _ = await setup(boundary, client, effects)
        original.tool_input_guardrails = [
            ToolInputGuardrail(
                guardrail_function=lambda _: ToolGuardrailFunctionOutput.raise_exception("existing")
            )
        ]
        agent.tools = [protect_function_tool(original, bindings=[binding])]
        with pytest.raises(ToolInputGuardrailTripwireTriggered) as failure:
            await Runner.run(agent, "Run", run_config=CONFIG)
        assert failure.value.output.output_info == "existing"
        assert effects == [] and boundary.requests == []


@pytest.mark.asyncio
async def test_concurrent_contexts_do_not_share_permission() -> None:
    boundary = Boundary("by_argument")
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects)
        agent.model = ScriptedModel(
            [
                ("record", '{"text":"safe"}', "call-safe"),
                ("record", '{"text":"BLOCK"}', "call-denied"),
            ]
        )
        with pytest.raises(ToolInputGuardrailTripwireTriggered):
            await Runner.run(agent, "Run", run_config=CONFIG)
        assert "BLOCK" not in effects and effects.count("safe") <= 1
        assert {r["step"]["context"]["norma"]["tool_call_id"] for r in boundary.requests} == {
            "call-safe",
            "call-denied",
        }


@pytest.mark.asyncio
async def test_stream_cancellation_while_check_pending() -> None:
    boundary = Boundary("pause")
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects)
        stream = Runner.run_streamed(agent, "Run", run_config=CONFIG)

        async def drain():
            return [event async for event in stream.stream_events()]

        task = asyncio.create_task(drain())
        await asyncio.wait_for(boundary.entered.wait(), 5)
        stream.cancel()
        await asyncio.wait_for(task, 5)
        boundary.release.set()
        assert effects == []


@pytest.mark.asyncio
async def test_stream_allow_uses_guarded_tool() -> None:
    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects)
        stream = Runner.run_streamed(agent, "Run", run_config=CONFIG)
        events = [event async for event in stream.stream_events()]
        assert events and effects == ["safe"]
        assert stream.tool_input_guardrail_results[0].output.output_info.decision.allowed


@pytest.mark.asyncio
async def test_handoff_checks_final_active_agent_binding() -> None:
    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        final, final_binding, original, _ = await setup(boundary, client, effects)
        initial = Agent(
            name="initial",
            handoffs=[final],
            model=ScriptedModel([("transfer_to_trusted", "{}", "handoff-1")]),
        )
        initial_binding = AgentBinding(agent=initial, client=client, agent_name="initial-agent")
        await initial_binding.initialize()
        final.tools = [protect_function_tool(original, bindings=[initial_binding, final_binding])]
        result = await Runner.run(initial, "Run", run_config=CONFIG)
        assert effects == ["safe"]
        record = result.tool_input_guardrail_results[0].output.output_info
        assert record.agent_name == "trusted-agent" and record.framework_agent_name == "trusted"
        assert boundary.requests[0]["agent_name"] == "trusted-agent"


@pytest.mark.asyncio
async def test_approval_resume_rechecks_before_effect() -> None:
    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, binding, original, _ = await setup(boundary, client, effects)
        original.needs_approval = True
        agent.tools = [protect_function_tool(original, bindings=[binding])]
        first = await Runner.run(agent, "Run", run_config=CONFIG)
        assert first.interruptions and effects == []
        resumed = first.to_state()
        resumed.approve(first.interruptions[0])
        boundary.mode = "deny"
        with pytest.raises(ToolInputGuardrailTripwireTriggered):
            await Runner.run(agent, resumed, run_config=CONFIG)
        assert effects == [] and boundary.requests


@pytest.mark.asyncio
async def test_postcheck_uses_original_proposal_after_tool_mutates_context() -> None:

    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, binding, _, _ = await setup(boundary, client, effects)

        @function_tool(failure_error_function=None)
        async def record(ctx: ToolContext[Any], text: str) -> str:
            effects.append(text)
            ctx.tool_arguments = '{"text":"changed"}'
            return text

        agent.tools = [protect_function_tool(record, bindings=[binding], check_output=True)]
        await Runner.run(agent, "Run", run_config=CONFIG)
        assert effects == ["safe"]
        assert [request["step"]["input"] for request in boundary.requests] == [{"text": "safe"}] * 2


@pytest.mark.asyncio
async def test_context_extractor_failure_blocks_before_http() -> None:
    boundary = Boundary()
    effects: list[str] = []

    def extract(_):
        raise ValueError("Sensitive context must not appear in model feedback")

    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects, context_extractor=extract)
        with pytest.raises(ToolInputGuardrailTripwireTriggered) as failure:
            await Runner.run(agent, "Run", run_config=CONFIG)
        assert failure.value.output.output_info.error == "ValueError"
        assert effects == [] and boundary.requests == []


def test_unsupported_tools_rejected() -> None:
    from agents import WebSearchTool

    with pytest.raises(TypeError, match="ordinary FunctionTool"):
        protect_function_tool(WebSearchTool(), bindings=[])
    nested = Agent(name="nested", model=ScriptedModel([])).as_tool(
        tool_name="nested", tool_description="Nested agent"
    )
    with pytest.raises(TypeError, match="ordinary FunctionTool"):
        protect_function_tool(nested, bindings=[])


@pytest.mark.asyncio
async def test_parallel_allowed_calls_each_execute_once() -> None:
    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects)
        agent.model = ScriptedModel(
            [
                ("record", '{"text":"first"}', "call-first"),
                ("record", '{"text":"second"}', "call-second"),
            ]
        )
        result = await Runner.run(agent, "Run", run_config=CONFIG)
        assert sorted(effects) == ["first", "second"]
        assert {r.output.output_info.tool_call_id for r in result.tool_input_guardrail_results} == {
            "call-first",
            "call-second",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['{"text":"a","text":"b"}', '{"text":NaN}', "null"])
async def test_malformed_arguments_never_execute(raw: str) -> None:
    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects)
        agent.model = ScriptedModel([("record", raw, "call-invalid")])
        with pytest.raises(Exception):
            await Runner.run(agent, "Run", run_config=CONFIG)
        assert effects == [] and boundary.requests == []


@pytest.mark.asyncio
async def test_reopened_client_requires_new_binding_initialization() -> None:
    boundary = Boundary()
    effects: list[str] = []
    client = NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    )
    async with client:
        agent, _, _, _ = await setup(boundary, client, effects)
    async with client:
        with pytest.raises(ToolInputGuardrailTripwireTriggered):
            await Runner.run(agent, "Run", run_config=CONFIG)
        assert effects == [] and boundary.requests == []


@pytest.mark.asyncio
async def test_stream_denial_raises_on_consumption_before_effect() -> None:
    boundary = Boundary("deny")
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects)
        stream = Runner.run_streamed(agent, "Run", run_config=CONFIG)
        with pytest.raises(ToolInputGuardrailTripwireTriggered):
            async for _ in stream.stream_events():
                pass
        assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["call_id", "agent", "arguments"])
async def test_identity_mutation_during_http_check_blocks(mutation: str) -> None:
    boundary = Boundary("pause")
    effects: list[str] = []
    contexts = []

    def capture(context):
        contexts.append(context)
        return {}

    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:
        agent, _, _, _ = await setup(boundary, client, effects, context_extractor=capture)
        running = asyncio.create_task(Runner.run(agent, "Run", run_config=CONFIG))
        await asyncio.wait_for(boundary.entered.wait(), 5)
        context = contexts[0]
        if mutation == "call_id":
            context.tool_call_id = "changed-call"
        elif mutation == "agent":
            context.agent = Agent(name="other", model=ScriptedModel([]))
        else:
            context.tool_arguments = '{"text":"changed"}'
        boundary.release.set()
        with pytest.raises(ToolInputGuardrailTripwireTriggered):
            await running
        assert effects == []


@pytest.mark.asyncio
async def test_transport_closed_after_precheck_blocks_effect() -> None:
    boundary = Boundary()
    effects: list[str] = []
    async with NormaClient(
        "https://tenant.example",
        agent_key="synthetic",
        transport=httpx.MockTransport(boundary.handle),
    ) as client:

        class ClosingHooks(RunHooks):
            async def on_tool_start(self, context, agent, tool):
                await client.http_client.aclose()

        agent, _, _, _ = await setup(boundary, client, effects)
        with pytest.raises(ToolInputGuardrailTripwireTriggered):
            await Runner.run(agent, "Run", run_config=CONFIG, hooks=ClosingHooks())
        assert effects == []
