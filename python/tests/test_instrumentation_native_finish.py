# Modified for Norma.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from agents import Agent as OpenAIAgent
from agents import RunConfig, Runner, function_tool
from agents.exceptions import ToolInputGuardrailTripwireTriggered, UserError
from agents.testing.model import ScriptedModel, assistant_message, function_call
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import norma
from norma.checkpoints import CheckError
from norma.models import ContinuationState, FinishVerdict

URL = "https://runtime.example.test/api/runtime"


def _verdict(
    kind: str, count: int, *, agent_generation: str | None = None
) -> dict[str, Any]:
    if kind == "hold":
        value = FinishVerdict(
            execution_contract="hosted-v1",
            verdict="hold",
            reason="Please correct the claim",
            reason_codes=["claim_contradicted"],
            held_by=[7],
            would_hold_by=[],
            continuation=ContinuationState(
                count=count, max=2, agent_generation=agent_generation
            ),
            agent_generation=agent_generation,
            allow_finish=False,
            outcome="held",
        )
    else:
        value = FinishVerdict(
            execution_contract="hosted-v1",
            verdict="pass",
            reason="Accepted",
            reason_codes=[],
            held_by=[],
            would_hold_by=[],
            continuation=ContinuationState(
                count=count, max=2, agent_generation=agent_generation
            ),
            agent_generation=agent_generation,
            allow_finish=True,
            outcome="completed",
        )
    return value.model_dump(mode="json")


def _finish_config(driver: str, adapter: str = "native") -> dict[str, Any]:
    return {
        "version": 2,
        "entrypoints": [
            {
                "target": "app.run",
                "turn": {
                    "session_id": {"path": ["args", 0, "session_id"]},
                    "turn_id": {"path": ["args", 0, "turn_id"]},
                    "finish": {"driver": driver, "adapter": adapter},
                },
            }
        ],
        "checkpoints": {},
    }


def test_native_tool_action_rejects_unretained_cwd_in_shared_parser() -> None:
    config = {
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {},
        "adapters": {
            "native": {
                "kind": "openai_agents",
                "agent": {"binding": "framework.agent"},
                "tools": {
                    "send": {
                        "tool": {"binding": "framework.tool"},
                        "action": {"kind": "edit", "cwd": "/tmp"},
                    }
                },
            }
        },
    }
    with pytest.raises(
        ValueError,
        match=r"adapters\.native\.tools\.send\.action \(constraint: additionalProperties\)",
    ):
        norma.parse_instrumentation(json.dumps(config))


@pytest.mark.asyncio
async def test_configured_openai_finish_preserves_tool_history_and_native_result() -> None:
    effects: list[str] = []
    verdicts: list[dict[str, Any]] = []

    def on_verdict(payload: dict[str, Any]) -> None:
        verdicts.append(payload)

    @function_tool
    def record(text: str) -> str:
        """Record one completed action."""
        effects.append(text)
        return f"recorded:{text}"

    agent = OpenAIAgent(
        name="configured-openai",
        model=ScriptedModel(
            [
                [function_call("record", {"text": "once"}, call_id="tool-1")],
                [assistant_message("First final claim", item_id="claim-1")],
                [assistant_message("Corrected final claim", item_id="claim-2")],
            ]
        ),
        tools=[record],
    )
    app = SimpleNamespace()

    async def run(request: dict[str, str]) -> Any:
        return await Runner.run(
            agent,
            request["prompt"],
            run_config=RunConfig(tracing_disabled=True),
        )

    app.run = run
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.method == "GET" and request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        body = json.loads(request.content)
        requests.append(body)
        step = body.get("step", {})
        if step.get("type") == "turn":
            turn = step["input"]
            count = turn["continuation"]["count"]
            return httpx.Response(
                200,
                json=_verdict(
                    "hold" if count == 0 else "pass",
                    count,
                    agent_generation="openai-generation-1",
                ),
            )
        return httpx.Response(
            200,
            json={
                "allowed": True,
                "confidence": 1,
                "matches": [],
                "non_matches": [],
                "errors": [],
                "execution_contract": "hosted-v1",
            },
        )

    config = _finish_config("openai_agents")
    config["adapters"] = {
        "native": {
            "kind": "openai_agents",
            "agent": {"binding": "framework.agent"},
            "runner": {"binding": "framework.runner"},
            "tools": {
                "record": {
                    "tool": {"binding": "tools.record"},
                    "action": {
                        "kind": "edit",
                        "target": {"path": ["args", 0, "text"]},
                        "command": "record",
                    },
                }
            },
        }
    }
    config["entrypoints"][0]["turn"]["finish"]["on_verdict"] = {"binding": "callbacks.on_verdict"}

    transport = httpx.MockTransport(handler)
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={
            "app": app,
            "framework": {"agent": agent, "runner": Runner},
            "tools": {"record": record},
            "callbacks": {"on_verdict": on_verdict},
        },
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": transport,
            "observability_enabled": False,
        },
    )
    try:
        result = await app.run(
            {"session_id": "openai-session", "turn_id": "turn-1", "prompt": "Run it"}
        )
        assert result.final_output == "Corrected final claim"
        assert len(effects) == 1
        turns = [
            body["step"]["input"] for body in requests if body.get("step", {}).get("type") == "turn"
        ]
        assert [item["continuation"]["count"] for item in turns] == [0, 1]
        assert turns[0]["continuation"] == {"count": 0, "max": 2}
        assert turns[1]["continuation"]["agent_generation"] == "openai-generation-1"
        assert verdicts and verdicts[-1]["agent_name"] == "agent-alpha"
        assert verdicts[-1]["result"] is result
        assert verdicts[-1]["turn"].final_message == "Corrected final claim"
        assert verdicts[-1]["turn"].continuation.count == 1
        assert [action.model_dump() for action in verdicts[-1]["turn"].actions] == [
            {
                "index": 0,
                "kind": "edit",
                "native_tool": "record",
                "target": "once",
                "command": "record",
                "status": "succeeded",
                "exit_code": None,
                "output_tail": None,
                "duration_ms": verdicts[-1]["turn"].actions[0].duration_ms,
            }
        ]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_openai_finish_blocks_continuation_after_session_close() -> None:
    agent = OpenAIAgent(
        name="closing-openai",
        model=ScriptedModel(
            [
                [assistant_message("First final claim", item_id="claim-1")],
                [assistant_message("Should not run after close", item_id="claim-2")],
            ]
        ),
        tools=[],
    )
    app = SimpleNamespace()

    async def run(request: dict[str, str]) -> Any:
        return await Runner.run(
            agent, request["prompt"], run_config=RunConfig(tracing_disabled=True)
        )

    app.run = run
    turn_started = asyncio.Event()
    release_turn = asyncio.Event()
    turn_bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.method == "GET" and request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        body = json.loads(request.content)
        if body.get("step", {}).get("type") == "turn":
            turn_bodies.append(body["step"]["input"])
            turn_started.set()
            await release_turn.wait()
            return httpx.Response(200, json=_verdict("hold", 0))
        return httpx.Response(
            200,
            json={
                "allowed": True,
                "confidence": 1,
                "matches": [],
                "non_matches": [],
                "errors": [],
                "execution_contract": "hosted-v1",
            },
        )

    config = _finish_config("openai_agents")
    config["adapters"] = {
        "native": {
            "kind": "openai_agents",
            "agent": {"binding": "framework.agent"},
            "runner": {"binding": "framework.runner"},
            "tools": {},
        }
    }
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app, "framework": {"agent": agent, "runner": Runner}},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
        },
    )
    pending = asyncio.create_task(
        app.run({"session_id": "close-session", "turn_id": "turn-1", "prompt": "Run"})
    )
    try:
        await asyncio.wait_for(turn_started.wait(), timeout=2)
        await configured.aclose()
        release_turn.set()
        with pytest.raises(CheckError, match="session changed or closed"):
            await pending
        assert len(turn_bodies) == 1
    finally:
        release_turn.set()
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_pydantic_finish_uses_native_modelretry_and_result() -> None:
    calls = 0
    verdicts: list[dict[str, Any]] = []

    def on_verdict(payload: dict[str, Any]) -> None:
        verdicts.append(payload)

    def model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart("First claim" if calls == 1 else "Corrected claim")])

    agent = PydanticAgent(FunctionModel(model))
    app = SimpleNamespace()

    async def run(request: dict[str, str]) -> Any:
        return await agent.run(request["prompt"])

    app.run = run
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.method == "GET" and request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        body = json.loads(request.content)
        requests.append(body)
        turn = body["step"]["input"]
        count = turn["continuation"]["count"]
        return httpx.Response(
            200,
            json=_verdict(
                "hold" if count == 0 else "pass",
                count,
                agent_generation="pydantic-generation-1",
            ),
        )

    config = _finish_config("pydantic_ai")
    config["adapters"] = {
        "native": {"kind": "pydantic_ai", "agent": {"binding": "framework.agent"}}
    }
    config["entrypoints"][0]["turn"]["finish"]["on_verdict"] = {"binding": "callbacks.on_verdict"}
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={
            "app": app,
            "framework": {"agent": agent},
            "callbacks": {"on_verdict": on_verdict},
        },
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
        },
    )
    try:
        result = await app.run(
            {"session_id": "pydantic-session", "turn_id": "turn-1", "prompt": "Run it"}
        )
        assert result.output == "Corrected claim"
        assert calls == 2
        turns = [body["step"]["input"] for body in requests]
        assert [item["continuation"]["count"] for item in turns] == [0, 1]
        assert turns[0]["continuation"] == {"count": 0, "max": 2}
        assert turns[1]["continuation"]["agent_generation"] == "pydantic-generation-1"
        assert verdicts and verdicts[-1]["agent_name"] == "agent-alpha"
        assert verdicts[-1]["result"] is result
        assert verdicts[-1]["turn"].final_message == "Corrected claim"
        assert verdicts[-1]["turn"].continuation.count == 1
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_pydantic_finish_blocks_retry_when_session_closes_during_check() -> None:
    calls = 0

    def model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart("First claim")])

    agent = PydanticAgent(FunctionModel(model))
    app = SimpleNamespace()

    async def run(request: dict[str, str]) -> Any:
        return await agent.run(request["prompt"])

    app.run = run
    turn_started = asyncio.Event()
    release_turn = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.method == "GET" and request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        turn_started.set()
        await release_turn.wait()
        return httpx.Response(200, json=_verdict("hold", 0))

    config = _finish_config("pydantic_ai")
    config["adapters"] = {
        "native": {"kind": "pydantic_ai", "agent": {"binding": "framework.agent"}}
    }
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app, "framework": {"agent": agent}},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
        },
    )
    pending = asyncio.create_task(
        app.run({"session_id": "pydantic-close", "turn_id": "turn-1", "prompt": "Run"})
    )
    try:
        await asyncio.wait_for(turn_started.wait(), timeout=2)
        await configured.aclose()
        release_turn.set()
        with pytest.raises(CheckError, match="session changed or closed"):
            await pending
        assert calls == 1
    finally:
        release_turn.set()
        await configured.aclose()


@pytest.mark.asyncio
async def test_native_tool_action_records_formatted_failure_without_reading_error_text() -> None:
    @function_tool
    def fail(text: str) -> str:
        """Demonstrate a framework-formatted tool failure."""
        raise ValueError(f"failure contains sensitive input: {text}")

    agent = OpenAIAgent(
        name="formatted-error-agent",
        model=ScriptedModel(
            [
                [function_call("fail", {"text": "API_KEY=private-value"}, call_id="fail-1")],
                [assistant_message("I reported the failure", item_id="reported-error")],
            ]
        ),
        tools=[fail],
    )
    app = SimpleNamespace()

    async def run() -> Any:
        return await Runner.run(
            agent, "Run the failing tool", run_config=RunConfig(tracing_disabled=True)
        )

    app.run = run
    turns: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.method == "GET" and request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        body = json.loads(request.content)
        if body.get("step", {}).get("type") == "turn":
            turns.append(body["step"]["input"])
            count = body["step"]["input"]["continuation"]["count"]
            return httpx.Response(200, json=_verdict("pass", count))
        return httpx.Response(
            200,
            json={
                "allowed": True,
                "confidence": 1,
                "matches": [],
                "non_matches": [],
                "errors": [],
                "execution_contract": "hosted-v1",
            },
        )

    config = {
        "version": 2,
        "entrypoints": [
            {
                "target": "app.run",
                "turn": {
                    "session_id": {"value": "failure-session"},
                    "turn_id": {"value": "failure-turn"},
                    "finish": {
                        "driver": "generic",
                        "final_message": {"value": "I reported the failure"},
                        "on_hold": "raise",
                    },
                },
            }
        ],
        "checkpoints": {},
        "adapters": {
            "native": {
                "kind": "openai_agents",
                "agent": {"binding": "framework.agent"},
                "tools": {
                    "fail": {
                        "tool": {"binding": "tools.fail"},
                        "action": {
                            "kind": "shell",
                            "command": {"path": ["input", "text"]},
                        },
                    }
                },
            }
        },
    }
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app, "framework": {"agent": agent}, "tools": {"fail": fail}},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
        },
    )
    try:
        result = await app.run()
        assert result.final_output == "I reported the failure"
        assert len(turns) == 1
        action = turns[0]["actions"][0]
        assert action == {
            "index": 0,
            "kind": "shell",
            "native_tool": "fail",
            "target": None,
                "command": "API_KEY=[REDACTED]",
            "status": "failed",
            "exit_code": None,
            "output_tail": "ValueError",
            "duration_ms": action["duration_ms"],
        }
        assert "private-value" not in json.dumps(turns[0])
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_native_tool_action_rejects_signature_params_before_http_or_mutation() -> None:
    @function_tool
    def record(text: str) -> str:
        """Record one native tool action."""
        return text

    agent = OpenAIAgent(name="native-params-agent", model=ScriptedModel([]), tools=[record])
    app = SimpleNamespace(run=lambda: "unused")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)

    config = {
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {},
        "adapters": {
            "native": {
                "kind": "openai_agents",
                "agent": {"binding": "framework.agent"},
                "tools": {
                    "record": {
                        "tool": {"binding": "tools.record"},
                        "action": {
                            "kind": "shell",
                            "command": {"path": ["params", "text"]},
                        },
                    }
                },
            }
        },
    }
    with pytest.raises(ValueError, match="unavailable for native tool arguments"):
        await norma.aconfigure(
            config,
            agent_name="agent-alpha",
            bindings={"app": app, "framework": {"agent": agent}, "tools": {"record": record}},
            connection={
                "runtime_url": URL,
                "agent_key": "key-alpha",
                "transport": httpx.MockTransport(handler),
                "observability_enabled": False,
            },
        )
    assert requests == []
    assert agent.tools == [record]


@pytest.mark.asyncio
async def test_native_tool_action_records_propagated_error_without_changing_exception() -> None:
    @function_tool(failure_error_function=None)
    def fail(text: str) -> str:
        """Propagate tool failures to the application."""
        raise ValueError(f"private failure: {text}")

    agent = OpenAIAgent(
        name="propagated-error-agent",
        model=ScriptedModel(
            [[function_call("fail", {"text": "DO_NOT_RECORD"}, call_id="fail-2")]]
        ),
        tools=[fail],
    )
    app = SimpleNamespace()

    async def run() -> str:
        try:
            await Runner.run(
                agent, "Run the failing tool", run_config=RunConfig(tracing_disabled=True)
            )
        except UserError as exc:
            return type(exc).__name__
        raise AssertionError("the native FunctionTool should propagate its original error")

    app.run = run
    turns: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.method == "GET" and request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        body = json.loads(request.content)
        if body.get("step", {}).get("type") == "turn":
            turns.append(body["step"]["input"])
            count = body["step"]["input"]["continuation"]["count"]
            return httpx.Response(200, json=_verdict("pass", count))
        return httpx.Response(
            200,
            json={
                "allowed": True,
                "confidence": 1,
                "matches": [],
                "non_matches": [],
                "errors": [],
                "execution_contract": "hosted-v1",
            },
        )

    config = {
        "version": 2,
        "entrypoints": [
            {
                "target": "app.run",
                "turn": {
                    "session_id": {"value": "propagated-session"},
                    "turn_id": {"value": "propagated-turn"},
                    "finish": {
                        "driver": "generic",
                        "final_message": {"value": "The app caught the tool error"},
                        "on_hold": "raise",
                    },
                },
            }
        ],
        "checkpoints": {},
        "adapters": {
            "native": {
                "kind": "openai_agents",
                "agent": {"binding": "framework.agent"},
                "tools": {
                    "fail": {
                        "tool": {"binding": "tools.fail"},
                        "action": {"kind": "other", "target": "explicitly declared"},
                    }
                },
            }
        },
    }
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app, "framework": {"agent": agent}, "tools": {"fail": fail}},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
        },
    )
    try:
        assert await app.run() == "UserError"
        assert len(turns) == 1
        action = turns[0]["actions"][0]
        assert action["status"] == "failed"
        assert action["output_tail"] == "ValueError"
        assert "DO_NOT_RECORD" not in json.dumps(turns[0])
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_native_tool_action_records_precheck_denial_without_effect() -> None:
    effects: list[str] = []

    @function_tool
    def delete(path: str) -> str:
        """Delete one requested file."""
        effects.append(path)
        return "deleted"

    agent = OpenAIAgent(
        name="denied-action-agent",
        model=ScriptedModel(
            [[function_call("delete", {"path": "important.txt"}, call_id="delete-1")]]
        ),
        tools=[delete],
    )
    app = SimpleNamespace()

    async def run() -> str:
        try:
            await Runner.run(agent, "Delete the file", run_config=RunConfig(tracing_disabled=True))
        except ToolInputGuardrailTripwireTriggered:
            return "blocked"
        raise AssertionError("the hosted precheck should block the native tool")

    app.run = run
    turns: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.method == "GET" and request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        body = json.loads(request.content)
        if body.get("step", {}).get("type") == "turn":
            turns.append(body["step"]["input"])
            count = body["step"]["input"]["continuation"]["count"]
            return httpx.Response(200, json=_verdict("pass", count))
        return httpx.Response(
            200,
            json={
                "confidence": 1,
                "allowed": False,
                "matches": [
                    {
                        "action": "deny",
                        "rule_id": 17,
                        "rule_name": "Delete approval",
                        "result": {"matched": True, "confidence": 1},
                    }
                ],
                "non_matches": [],
                "errors": [],
                "execution_contract": "hosted-v1",
            },
        )

    config = {
        "version": 2,
        "entrypoints": [
            {
                "target": "app.run",
                "turn": {
                    "session_id": {"value": "denied-session"},
                    "turn_id": {"value": "denied-turn"},
                    "finish": {
                        "driver": "generic",
                        "final_message": {"value": "The deletion was blocked"},
                        "on_hold": "raise",
                    },
                },
            }
        ],
        "checkpoints": {},
        "adapters": {
            "native": {
                "kind": "openai_agents",
                "agent": {"binding": "framework.agent"},
                "tools": {
                    "delete": {
                        "tool": {"binding": "tools.delete"},
                        "action": {"kind": "edit", "target": {"value": "important.txt"}},
                    }
                },
            }
        },
    }
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app, "framework": {"agent": agent}, "tools": {"delete": delete}},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
        },
    )
    try:
        assert await app.run() == "blocked"
        assert effects == []
        assert turns and turns[0]["actions"], turns
        assert turns[0]["actions"][0]["status"] == "denied"
        assert turns[0]["actions"][0]["target"] == "important.txt"
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_invalid_openai_runner_binding_fails_before_http_or_native_mutation() -> None:
    @function_tool
    def record(text: str) -> str:
        """Record one completed action."""
        return text

    agent = OpenAIAgent(name="preflight", model=ScriptedModel([]), tools=[record])
    app = SimpleNamespace()

    async def run(request: dict[str, str]) -> Any:
        return await Runner.run(agent, request["prompt"])

    app.run = run
    requests: list[httpx.Request] = []
    config = _finish_config("openai_agents")
    config["adapters"] = {
        "native": {
            "kind": "openai_agents",
            "agent": {"binding": "framework.agent"},
            "runner": {"binding": "framework.runner"},
            "tools": {},
        }
    }
    original_run = vars(Runner)["run"]
    original_tool = agent.tools[0]
    with pytest.raises(ValueError, match="runner binding must resolve to Runner"):
        await norma.aconfigure(
            config,
            agent_name="agent-alpha",
            bindings={"app": app, "framework": {"agent": agent, "runner": object()}},
            connection={
                "runtime_url": URL,
                "agent_key": "key-alpha",
                "transport": httpx.MockTransport(
                    lambda request: requests.append(request) or httpx.Response(500)
                ),
                "observability_enabled": False,
            },
        )
    assert requests == []
    assert agent.tools == [original_tool]
    assert vars(Runner)["run"] is original_run


@pytest.mark.asyncio
async def test_openai_finish_blocks_runner_for_unbound_agent_before_model_call() -> None:
    selected = OpenAIAgent(name="selected", model=ScriptedModel([]), tools=[])
    unbound = OpenAIAgent(name="unbound", model=ScriptedModel([]), tools=[])
    app = SimpleNamespace()

    async def run(request: dict[str, str]) -> Any:
        return await Runner.run(
            unbound,
            request["prompt"],
            run_config=RunConfig(tracing_disabled=True),
        )

    app.run = run
    config = _finish_config("openai_agents")
    config["adapters"] = {
        "native": {
            "kind": "openai_agents",
            "agent": {"binding": "framework.agent"},
            "runner": {"binding": "framework.runner"},
            "tools": {},
        }
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.method == "GET" and request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        raise AssertionError("A mismatched native Agent must not reach a check or effect")

    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={
            "app": app,
            "framework": {"agent": selected, "runner": Runner},
        },
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
        },
    )
    try:
        with pytest.raises(CheckError, match="not bound to the selected adapter"):
            await app.run(
                {"session_id": "wrong-agent", "turn_id": "turn-1", "prompt": "Run"}
            )
        assert all(request.method == "GET" for request in requests)
    finally:
        await configured.aclose()
