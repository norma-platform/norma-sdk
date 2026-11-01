# Modified for Norma.
"""Exercise installed Norma with real Google ADK Runner; no inference or sockets.

Copy this file outside the checkout, then run with an interpreter containing the
built Norma wheel, google-adk and pytest/pytest-asyncio. Neither SDK nor ADK is mocked.
Only the hosted HTTP boundary uses httpx.MockTransport and BaseLlm is deterministic.
"""

# Install the network guard before importing framework/dependency modules.
# ruff: noqa: E402
from __future__ import annotations

import importlib.metadata
import json
import os
import sys
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

for key in list(os.environ):
    if key.startswith(("GOOGLE_", "GEMINI_", "OTEL_")):
        del os.environ[key]


def forbid_network(event: str, args: tuple[Any, ...]) -> None:
    if event in ("socket.connect", "socket.getaddrinfo", "socket.bind"):
        raise AssertionError(f"Unexpected network operation: {event}")


sys.addaudithook(forbid_network)
import httpx
import pytest
from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import PrivateAttr

import norma
from norma._state import get_state
from norma._state import state as legacy_state
from norma.integrations.google_adk import NormaPlugin

NAME = "adk-real-agent"


class ScriptedModel(BaseLlm):
    model: str = "deterministic-local"
    _calls: int = PrivateAttr(default=0)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._calls += 1
        if self._calls == 1:
            part = types.Part(
                function_call=types.FunctionCall(name="record_action", args={"value": "ok"})
            )
        else:
            part = types.Part(text="Completed locally")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class HostedBoundary:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.steps: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.check_headers: list[str] = []
        self.registrations = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "tenant.example", request.url
        path = request.url.path
        if path.endswith("/register"):
            self.registrations += 1
            if self.mode == "init_failure" or (
                self.mode == "registration_failure" and self.registrations > 1
            ):
                return httpx.Response(403, json={"detail": "Registration rejected"})
            if self.mode == "registration_reinit" and self.registrations == 2:
                norma.init(
                    NAME,
                    runtime_url="https://tenant.example",
                    agent_key="replacement-key",
                    observability_enabled=False,
                    policy_refresh_interval_seconds=0,
                )
            self.steps.extend(json.loads(request.content).get("steps", []))
            return httpx.Response(200, json={"created": False, "rules": []})
        if path.endswith("/agents/" + NAME):
            return httpx.Response(200, json={"agent": {"agent_name": NAME}, "steps": self.steps})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if not path.endswith("/checks"):
            raise AssertionError(f"Unexpected request {path}")
        data = json.loads(request.content)
        self.requests.append(data)
        self.check_headers.append(request.headers.get("X-API-Key", ""))
        if self.mode == "outage":
            raise httpx.ConnectError("Simulated hosted outage", request=request)
        if data.get("step", {}).get("type") == "turn":
            return httpx.Response(
                200,
                json={
                    "execution_contract": "hosted-v1",
                    "verdict": "pass",
                    "reason": "Complete",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": data["step"]["input"]["continuation"],
                    "allow_finish": True,
                    "outcome": "completed",
                },
            )
        result: dict[str, Any] = {
            "allowed": True,
            "confidence": 1,
            "execution_contract": "hosted-v1",
        }
        stage = data.get("stage", data.get("step", {}).get("stage"))
        step = data.get("step", {})
        kind = data.get("step_type", step.get("type"))
        wanted = self.mode.split("_")
        if (
            len(wanted) == 3
            and wanted[0] in ("deny", "steer")
            and kind == wanted[1]
            and stage == wanted[2]
        ):
            result.update(allowed=False, matches=[self.match(wanted[0])])
        if self.mode == "post_outage" and kind == "tool" and stage == "post":
            raise httpx.ConnectError("Post-action hosted outage", request=request)
        if self.mode == "missing_proof":
            result.pop("execution_contract")
        if result.get("matches"):
            result["recovery_feedback"] = [
                {
                    "rule_id": 7,
                    "check_id": "check-7",
                    "outcome": "matched",
                    "reason": "criterion_triggered",
                    "condition_ids": ["condition-7"],
                }
            ]
        if self.mode == "embedded_error_steer":
            result.update(
                allowed=False,
                matches=[self.match("steer")],
                non_matches=[
                    {
                        **self.match("observe"),
                        "result": {
                            "matched": False,
                            "confidence": 1,
                            "error": "evaluator unavailable",
                        },
                    }
                ],
            )
        return httpx.Response(200, json=result)

    @staticmethod
    def match(action: str) -> dict[str, Any]:
        return {
            "rule_id": 7,
            "rule_name": "bounded-action",
            "check_id": "check-7",
            "action": action,
            "steering_context": {"message": "Revise explicitly"},
            "result": {"matched": True, "confidence": 1, "message": "Rule triggered"},
        }


async def exercise(
    monkeypatch: pytest.MonkeyPatch, mode: str, *, bind: bool = True
) -> dict[str, Any]:
    boundary = HostedBoundary(mode)
    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(boundary.handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    await norma.ashutdown()
    try:
        norma.init(
            NAME,
            runtime_url="https://tenant.example",
            agent_key="synthetic-adk-test",
            observability_enabled=False,
            policy_refresh_interval_seconds=0,
        )
    except Exception:
        if mode != "init_failure":
            raise
    effects: list[str] = []
    callbacks: list[tuple[dict[str, Any], Any]] = []

    def record_action(value: str) -> dict[str, str]:
        """Record a local deterministic action."""
        effects.append(value)
        return {"recorded": value}

    def extract(**kwargs: Any) -> dict[str, Any]:
        if mode == "context_failure":
            raise ValueError("Required caller context unavailable")
        return {"tenant_evidence": "fixture"}

    model = ScriptedModel()
    agent = Agent(name=NAME.replace("-", "_"), model=model, tools=[record_action])
    plugin = NormaPlugin(
        NAME,
        context_extractor=extract,
        on_violation_callback=lambda info, result: callbacks.append((info, result)),
    )
    failure = None
    events = []
    service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="norma_test", session_service=service, plugins=[plugin])
    try:
        if bind:
            plugin.bind(agent)
        if mode in ("warm_reinit_agent", "warm_reinit_key", "warm_reinit_same"):
            norma.init(
                "another-adk-agent" if mode == "warm_reinit_agent" else NAME,
                runtime_url="https://tenant.example",
                agent_key="replacement-key" if mode == "warm_reinit_key" else "synthetic-adk-test",
                observability_enabled=False,
                policy_refresh_interval_seconds=0,
            )
        await service.create_session(app_name="norma_test", user_id="fixture", session_id="one")
        events = [
            event
            async for event in runner.run_async(
                user_id="fixture",
                session_id="one",
                new_message=types.Content(
                    role="user", parts=[types.Part(text="Do the local action")]
                ),
            )
        ]
    except Exception as exc:
        failure = exc
    finally:
        await plugin.close()
        await norma.ashutdown()
    return {
        "model_calls": model._calls,
        "effects": effects,
        "callbacks": callbacks,
        "boundary": boundary,
        "failure": failure,
        "events": events,
    }


@pytest.mark.asyncio
async def test_real_runner_uses_configured_entrypoint_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync ADK bind and async Runner stay inside the configured session."""
    await norma.ashutdown()
    boundary = HostedBoundary("allow")
    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(boundary.handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    app = SimpleNamespace()
    effects: list[str] = []
    model = ScriptedModel()
    agent = Agent(name=NAME.replace("-", "_"), model=model, tools=[])

    def record_action(value: str) -> dict[str, str]:
        """Record a deterministic local action."""
        effects.append(value)
        return {"recorded": value}

    agent.tools = [*agent.tools, record_action]
    service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="norma_configured",
        session_service=service,
    )

    async def run() -> AsyncGenerator[Any, None]:
        await service.create_session(
            app_name="norma_configured", user_id="fixture", session_id="configured"
        )
        async for event in runner.run_async(
            user_id="fixture",
            session_id="configured",
            new_message=types.Content(role="user", parts=[types.Part(text="Run locally")]),
        ):
            yield event

    app.run = run
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {},
            "adapters": {
                "adk": {
                    "kind": "google_adk",
                    "runner": {"binding": "framework.runner"},
                    "enabled_hooks": ["before_model", "before_tool"],
                }
            },
        },
        agent_name=NAME,
        bindings={"app": app, "framework": {"runner": runner}},
        connection={
            "runtime_url": "https://tenant.example",
            "agent_key": "configured-adk-key",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    assert boundary.registrations == 0, "bootstrap must use GET-only authentication"
    try:
        events = []
        async for event in app.run():
            events.append(event)
            assert get_state() is legacy_state, "stream wrapper leaked session scope past a yield"
        assert events
        assert model._calls == 2
        assert effects == ["ok"]
        assert boundary.requests
        assert set(boundary.check_headers) == {"configured-adk-key"}
    finally:
        await configured.aclose()
        await norma.ashutdown()


@pytest.mark.asyncio
async def test_real_runner_streaming_entrypoint_closes_after_early_consumer_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native ADK stream uses configured scope per advance and cleans up when closed."""
    await norma.ashutdown()
    boundary = HostedBoundary("allow")
    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(boundary.handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    app = SimpleNamespace()
    model = ScriptedModel()
    agent = Agent(name=NAME.replace("-", "_"), model=model, tools=[])
    service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="norma_stream", session_service=service)

    async def stream() -> AsyncGenerator[Any, None]:
        await service.create_session(
            app_name="norma_stream", user_id="fixture", session_id="partial"
        )
        async for event in runner.run_async(
            user_id="fixture",
            session_id="partial",
            new_message=types.Content(role="user", parts=[types.Part(text="Start")]),
        ):
            yield event

    app.stream = stream
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.stream"],
            "checkpoints": {},
            "adapters": {
                "adk": {
                    "kind": "google_adk",
                    "runner": {"binding": "framework.runner"},
                    "enabled_hooks": ["before_model", "before_tool"],
                }
            },
        },
        agent_name=NAME,
        bindings={"app": app, "framework": {"runner": runner}},
        connection={
            "runtime_url": "https://tenant.example",
            "agent_key": "configured-adk-key",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        stream_iterator = app.stream()
        first_event = await anext(stream_iterator)
        assert first_event is not None
        assert get_state() is legacy_state, "stream wrapper leaked session scope past a yield"
        await stream_iterator.aclose()
        assert get_state() is legacy_state
        assert boundary.requests
        assert set(boundary.check_headers) == {"configured-adk-key"}
    finally:
        await configured.aclose()
        assert not runner.plugin_manager.plugins
        await norma.ashutdown()


@pytest.mark.asyncio
async def test_real_runner_run_async_entrypoint_finishes_selected_stream_text_without_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public stream finish works on ADK's native Runner.run_async generator."""
    await norma.ashutdown()
    boundary = HostedBoundary("allow")
    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(boundary.handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    model = ScriptedModel()
    agent = Agent(name=NAME.replace("-", "_"), model=model, tools=[])

    def record_action(value: str) -> dict[str, str]:
        return {"recorded": value}

    agent.tools = [*agent.tools, record_action]
    service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="norma_stream_finish", session_service=service)

    class TextSelector:
        @staticmethod
        def select(invocation: dict[str, object]) -> str | None:
            event = invocation["result"]
            content = getattr(event, "content", None)
            parts = getattr(content, "parts", ())
            text = "".join(
                part.text for part in parts if isinstance(getattr(part, "text", None), str)
            )
            return text or None

    callbacks = TextSelector()
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "framework.runner.run_async",
                    "turn": {
                        "session_id": {"path": ["kwargs", "session_id"]},
                        "turn_id": {"value": str(uuid.uuid4())},
                        "finish": {
                            "stream": {
                                "text": {"provider": "callbacks.select"},
                                "missing": "skip",
                            },
                            "on_hold": "raise",
                        },
                    },
                }
            ],
            "checkpoints": {},
            "adapters": {
                "adk": {
                    "kind": "google_adk",
                    "runner": {"binding": "framework.runner"},
                    "enabled_hooks": ["before_model", "before_tool"],
                }
            },
        },
        agent_name=NAME,
        bindings={"framework": {"runner": runner}, "callbacks": callbacks},
        connection={
            "runtime_url": "https://tenant.example",
            "agent_key": "configured-adk-key",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        await service.create_session(
            app_name="norma_stream_finish", user_id="fixture", session_id="stream-finish"
        )
        events = [
            event
            async for event in runner.run_async(
                user_id="fixture",
                session_id="stream-finish",
                new_message=types.Content(
                    role="user", parts=[types.Part(text="Run the deterministic local agent")]
                ),
            )
        ]
        assert events
        assert model._calls == 2
        turn_requests = [
            request
            for request in boundary.requests
            if request.get("step", {}).get("type") == "turn"
        ]
        assert len(turn_requests) == 1
        assert turn_requests[0]["step"]["input"]["final_message"] == "Completed locally"
        assert boundary.check_headers[-1] == "configured-adk-key"
    finally:
        await configured.aclose()
        assert not runner.plugin_manager.plugins
        await norma.ashutdown()


@pytest.mark.asyncio
async def test_real_runner_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await exercise(monkeypatch, "allow")
    assert result["failure"] is None, result["failure"]
    assert result["model_calls"] == 2
    assert result["effects"] == ["ok"]
    assert len(result["boundary"].requests) == 6, result["boundary"].requests
    assert result["boundary"].registrations >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "deny_llm_pre",
        "steer_llm_pre",
        "embedded_error_steer",
        "outage",
        "missing_proof",
        "init_failure",
        "context_failure",
        "registration_failure",
        "registration_reinit",
        "warm_reinit_agent",
        "warm_reinit_key",
        "warm_reinit_same",
    ],
)
async def test_real_runner_no_unprotected_model(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    result = await exercise(
        monkeypatch, mode, bind=mode not in ("init_failure", "registration_failure")
    )
    assert result["model_calls"] == 0, result
    assert result["effects"] == [], result
    assert result["failure"] is not None or result["events"]
    if mode.startswith(("deny_", "steer_")):
        info, callback = result["callbacks"][0]
        assert callback["rule_id"] == 7 and callback["rule_name"] == "bounded-action"
        assert callback["check_id"] == "check-7" and callback["stage"] == "pre"
        assert info["stage"] == "pre"
        assert callback["recovery_feedback"][0].rule_id == 7
        assert callback["recovery_feedback"][0].check_id == "check-7"
        if mode.startswith("steer"):
            assert "Revise explicitly" in str(result["events"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["deny_tool_pre", "steer_tool_pre", "deny_llm_post", "steer_llm_post"]
)
async def test_real_runner_blocks_tools(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    result = await exercise(monkeypatch, mode)
    assert result["failure"] is None, result["failure"]
    assert result["effects"] == [], result
    assert result["model_calls"] >= 1
    assert result["callbacks"], result
    assert result["callbacks"][0][1]["stage"] == mode.split("_")[-1]
    assert result["callbacks"][0][1]["check_id"] == "check-7"
    assert result["callbacks"][0][1]["recovery_feedback"][0].condition_ids == ["condition-7"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deny_tool_post", "steer_tool_post"])
async def test_real_runner_post_cannot_undo_effect(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    result = await exercise(monkeypatch, mode)
    assert result["failure"] is None, result["failure"]
    assert result["effects"] == ["ok"]
    assert result["callbacks"][0][1]["stage"] == "post"


def test_unknown_hook_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown Google ADK hooks"):
        NormaPlugin(NAME, enabled_hooks={"before_modle"})


def test_bind_requires_initialized_session() -> None:
    norma.shutdown()
    with pytest.raises(RuntimeError, match="norma.init"):
        NormaPlugin(NAME).bind(Agent(name="cold_agent", model=ScriptedModel()))


@pytest.mark.asyncio
async def test_post_outage_does_not_repeat_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await exercise(monkeypatch, "post_outage")
    assert result["failure"] is None
    assert result["effects"] == ["ok"]
    assert "could not evaluate" in str(result["events"])


if __name__ == "__main__":
    print("Google ADK:", importlib.metadata.version("google-adk"))
    print("Norma:", norma.__file__)
    raise SystemExit(pytest.main([__file__, "-q"]))
