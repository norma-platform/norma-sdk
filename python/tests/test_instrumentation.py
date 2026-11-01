# Modified for Norma.
from __future__ import annotations

import asyncio
import functools
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import norma
from norma.client import NormaClient

URL = "https://runtime.example.test/api/runtime"
SAFE = {"allowed": True, "confidence": 1.0, "execution_contract": "hosted-v1"}
_CONFORMANCE_PATH = (
    Path(__file__).parents[2] / "schema" / "fixtures" / "instrumentation-conformance.json"
)
_CONFORMANCE_CASES = json.loads(_CONFORMANCE_PATH.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _CONFORMANCE_CASES, ids=lambda case: case["name"])
def test_shared_parser_conformance_fixture(case: dict[str, object]) -> None:
    if not case["valid"]:
        with pytest.raises(ValueError):
            norma.parse_instrumentation(str(case["source"]), format=str(case["format"]))
        return
    assert (
        norma.parse_instrumentation(str(case["source"]), format=str(case["format"]))
        == case["normalized"]
    )


def test_json_and_yaml_parsing_are_equivalent() -> None:
    json_config = norma.parse_instrumentation(
        '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}'
    )
    yaml_config = norma.parse_instrumentation(
        "version: 1\nentrypoints:\n  - app.run\ncheckpoints: {}\n", "yaml"
    )
    assert json_config == yaml_config


@pytest.mark.asyncio
async def test_configure_characterizes_preconfigure_raw_alias_as_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterize preconfigure raw aliases; bind tools after setup or decorate directly."""
    app = SimpleNamespace()
    checked: list[dict[str, object]] = []
    actions: list[str] = []

    async def configured_action(value: str) -> str:
        actions.append(f"configured:{value}")
        return value

    captured_action = configured_action

    @norma.checkpoint(step_name="direct-action", step_type="tool")
    async def direct_action(value: str) -> str:
        actions.append(f"direct:{value}")
        return value

    async def run_captured(value: str) -> str:
        return await captured_action(value)

    async def run_direct(value: str) -> str:
        return await direct_action(value)

    async def run_current(value: str) -> str:
        return await app.action(value)

    app.action = configured_action
    app.run_captured = run_captured
    app.run_direct = run_direct
    app.run_current = run_current

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            checked.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run_captured", "app.run_current", "app.run_direct"],
            "checkpoints": {"action": {"target": "app.action"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run_captured("captured") == "captured"
        captured_checks = len(checked)
        assert await app.run_current("current") == "current"
        current_checks = len(checked) - captured_checks
        assert await app.run_direct("decorated") == "decorated"
        decorator_checks = len(checked) - captured_checks - current_checks
        # Config patches the declared attribute; it cannot rewrite arbitrary
        # function references copied before configuration.
        assert (captured_checks, current_checks, decorator_checks) == (0, 2, 2)
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_replaced_checkpoint_slot_blocks_entrypoint_and_agent_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    effects: list[str] = []
    checks: list[dict[str, object]] = []

    async def action() -> str:
        effects.append("original")
        return "original"

    async def run() -> str:
        return await app.action()

    async def replacement() -> str:
        effects.append("replacement")
        return "replacement"

    app.action, app.run = action, run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run"],
            "checkpoints": {"action": {"target": "app.action"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    installed_action, installed_run = app.action, app.run
    app.action = replacement
    errors: list[str] = []
    try:
        try:
            await app.run()
        except norma.CheckError as exc:
            errors.append(str(exc))
        try:
            with configured.scope("agent-alpha"):
                await app.action()
        except norma.CheckError as exc:
            errors.append(str(exc))
        app.action = installed_action

        async def replacement_entrypoint() -> str:
            effects.append("replacement-entrypoint")
            return "replacement-entrypoint"

        app.run = replacement_entrypoint
        try:
            await installed_run()
        except norma.CheckError as exc:
            errors.append(str(exc))
        assert len(errors) == 3
        assert effects == []
        assert checks == []
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_declarative_and_decorated_checkpoints_enforce_current_agent_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SimpleNamespace()
    checked_agents: list[str] = []
    effects: list[str] = []

    async def configured_action() -> str:
        effects.append("configured")
        return "configured"

    @norma.checkpoint(step_name="direct-action", step_type="tool")
    async def direct_action() -> str:
        effects.append("direct")
        return "direct"

    async def run_alpha() -> str:
        return await app.action()

    async def run_bravo() -> str:
        return "bravo"

    app.action, app.run_alpha, app.run_bravo = configured_action, run_alpha, run_bravo
    alpha = tmp_path / "agent-alpha.json"
    alpha.write_text(
        json.dumps(
            {
                "version": 1,
                "entrypoints": ["app.run_alpha"],
                "checkpoints": {"action": {"target": "app.action"}},
            }
        ),
        encoding="utf-8",
    )
    _write_empty_config(tmp_path, "agent-bravo", "app.run_bravo")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if path.endswith("/agents/agent-bravo"):
            return httpx.Response(200, json={"agent_name": "agent-bravo"})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            checked_agents.append(json.loads(request.content)["agent_name"])
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        tmp_path,
        bindings={"app": app},
        connection={"runtime_url": URL, "refresh_interval_seconds": 0},
        connections={
            "agent-alpha": {"agent_key": "key-alpha", "observability_enabled": False},
            "agent-bravo": {"agent_key": "key-bravo", "observability_enabled": False},
        },
    )
    installed_checkpoint = app.action
    try:
        with pytest.raises(norma.CheckError, match="entrypoint scope"):
            await installed_checkpoint()
        with configured.scope("agent-bravo"):
            with pytest.raises(norma.CheckError, match="not configured for Agent"):
                await installed_checkpoint()
            assert await direct_action() == "direct"
        assert checked_agents == ["agent-bravo", "agent-bravo"]
        assert effects == ["direct"]
    finally:
        await configured.aclose()

    with pytest.raises(norma.CheckError, match="entrypoint scope"):
        await installed_checkpoint()
    with pytest.raises(norma.CheckError, match="not initialized"):
        await direct_action()


@pytest.mark.asyncio
async def test_startup_rule_binding_and_mapped_check_inputs_keep_distinct_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    rules_read: list[dict[str, object]] = []
    checks: list[dict[str, object]] = []

    async def single(payload: dict[str, str]) -> str:
        return payload["recipient"]

    async def multi(recipient: str = "default@example.test", body: str = "hello") -> str:
        return f"{recipient}:{body}"

    async def defaulted(recipient: str = "default@example.test", body: str = "hello") -> str:
        return f"{recipient}:{body}"

    @norma.checkpoint(step_name="direct-multi", step_type="tool")
    async def direct_multi(recipient: str = "default@example.test", body: str = "hello") -> str:
        return f"{recipient}:{body}"

    async def run() -> tuple[str, str, str, str]:
        return (
            await app.single({"recipient": "single@example.test"}),
            await app.multi(body="hello"),
            await app.defaulted(body="hello"),
            await direct_multi(body="hello"),
        )

    app.single, app.multi, app.defaulted, app.run = single, multi, defaulted, run
    rule = {
        "id": 21,
        "name": "recipient-binding",
        "rule": {
            "execution": "server",
            "condition": {
                "selector": {"path": "*"},
                "evaluator": {
                    "name": "semantic",
                    "config": {
                        "bindings": [
                            {"name": "recipient", "path": "input.recipient", "required": True}
                        ]
                    },
                },
            },
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if path.endswith("/rules"):
            rules_read.extend([rule])
            return httpx.Response(200, json={"rules": [rule]})
        if path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {
                "single": {
                    "target": "app.single",
                    "input_from": {"path": ["input"]},
                },
                "multi": {"target": "app.multi", "input_from": "all_kwargs"},
                "defaulted": {"target": "app.defaulted"},
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run() == (
            "single@example.test",
            "default@example.test:hello",
            "default@example.test:hello",
            "default@example.test:hello",
        )
        assert len(rules_read) == 1
        assert (
            rules_read[0]["rule"]["condition"]["evaluator"]["config"]["bindings"][0]["path"]
            == "input.recipient"
        )
        inputs_by_step = {
            item["step"]["name"]: item["step"]["input"] for item in checks if item["stage"] == "pre"
        }
        assert inputs_by_step == {
            "single": {"recipient": "single@example.test"},
            "multi": {"body": "hello"},
            "defaulted": {"recipient": "default@example.test", "body": "hello"},
            "direct-multi": {"recipient": "default@example.test", "body": "hello"},
        }
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_state_provider_registry_reference_is_an_exact_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    app.action = lambda: "done"
    app.run = lambda: "run"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    with pytest.raises(
        ValueError,
        match=(
            r"Checkpoint 'action' source at /checkpoints/action/state_provider "
            r"must reference an exact callable key in the providers registry"
        ),
    ):
        await norma.aconfigure(
            {
                "version": 1,
                "entrypoints": ["app.run"],
                "checkpoints": {
                    "action": {"target": "app.action", "state_provider": "providers.snapshot"}
                },
            },
            agent_name="agent-alpha",
            bindings={"app": app},
            providers={"snapshot": lambda: {"safe": True}},
            connection={"runtime_url": URL, "agent_key": "key-alpha"},
        )
    assert requests == []


@pytest.mark.asyncio
async def test_value_source_provider_reference_uses_bindings_not_provider_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    app.action = lambda: "done"
    app.run = lambda: "run"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    with pytest.raises(
        ValueError,
        match=(
            r"Checkpoint 'action' source at /checkpoints/action/input_from must be a callable "
            r"dotted binding reference in bindings \(ValueSource provider\)"
        ),
    ):
        await norma.aconfigure(
            {
                "version": 2,
                "entrypoints": ["app.run"],
                "checkpoints": {
                    "action": {
                        "target": "app.action",
                        "input_from": {"provider": "providers.snapshot"},
                    }
                },
            },
            agent_name="agent-alpha",
            bindings={"app": app},
            providers={"snapshot": lambda: {"safe": True}},
            connection={"runtime_url": URL, "agent_key": "key-alpha"},
        )
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_field", "stage", "expected_checks", "expected_actions"),
    [
        ("input_from", "pre", 0, 0),
        ("state_from", "pre", 0, 0),
        ("state_provider", "pre", 0, 0),
        ("output_from", "post", 1, 1),
    ],
)
async def test_value_source_provider_errors_are_sanitized_with_config_location(
    monkeypatch: pytest.MonkeyPatch,
    source_field: str,
    stage: str,
    expected_checks: int,
    expected_actions: int,
) -> None:
    app = SimpleNamespace()
    calls: list[str] = []
    checks: list[dict[str, object]] = []
    secret = "provider-error-must-not-leak"

    async def action() -> str:
        calls.append("action")
        return "result"

    async def run() -> str:
        return await app.send()

    def fail_source(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError(secret)

    app.send, app.run, app.fail_source = action, run, fail_source

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    checkpoint_config: dict[str, object] = {"target": "app.send"}
    if source_field == "state_provider":
        checkpoint_config[source_field] = "snapshot"
        providers = {"snapshot": fail_source}
    else:
        checkpoint_config[source_field] = {"provider": "app.fail_source"}
        providers = None
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {"send": checkpoint_config},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        providers=providers,
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        with pytest.raises(norma.CheckError) as error:
            await app.run()
        assert error.value.stage == stage
        assert f"Checkpoint 'send' {stage}-check source '{source_field}'" in str(error.value)
        assert f"/checkpoints/send/{source_field}" in str(error.value)
        assert secret not in str(error.value)
        assert error.value.__cause__ is None
        assert len(checks) == expected_checks
        assert calls == ["action"] * expected_actions
    finally:
        await configured.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "a", "b", "context_from", "should_pass"),
    [
        (
            "params-scalar",
            "x",
            "private-context-value",
            {"path": ["params", "b"]},
            False,
        ),
        (
            "input-scalar",
            "private-input-value",
            "y",
            {"path": ["input"]},
            False,
        ),
        (
            "params-object",
            "x",
            {"note": "dynamic-params"},
            {"path": ["params", "b"]},
            True,
        ),
        (
            "input-object",
            {"note": "dynamic-input"},
            "y",
            {"path": ["input"]},
            True,
        ),
        ("kwargs-object", "x", "y", {"path": ["kwargs"]}, True),
        ("literal-object", "x", "y", {"value": {"note": "static"}}, True),
    ],
)
async def test_context_from_requires_object_and_reports_source_location(
    case: str,
    a: object,
    b: object,
    context_from: dict[str, object],
    should_pass: bool,
) -> None:
    checkpoints: list[dict[str, object]] = []
    actions: list[str] = []

    async def dummy_tool(a: object, b: object) -> str:
        actions.append("dummy_tool")
        return f"{a}-{b}"

    async def run_dummy(a: object, b: object) -> str:
        # Exercise the configured wrapper through its public module attribute.
        return await app.dummy_tool(a=a, b=b)

    app = SimpleNamespace(run_dummy=run_dummy)
    app.dummy_tool = dummy_tool

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            checkpoints.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [{"target": "app.run_dummy"}],
            "checkpoints": {
                "dummy_tool": {
                    "target": "app.dummy_tool",
                    "step_type": "tool",
                    "input_from": "all_kwargs",
                    "context_from": context_from,
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        if not should_pass:
            with pytest.raises(norma.CheckError) as error:
                await app.run_dummy(a=a, b=b)
            assert error.value.stage == "pre"
            message = str(error.value)
            assert "Checkpoint 'dummy_tool'" in message
            assert "pre-check" in message
            assert "context_from" in message
            assert "/checkpoints/dummy_tool/context_from" in message
            assert "must resolve to a JSON object" in message
            assert "private-context-value" not in message
            assert "private-input-value" not in message
            assert error.value.__cause__ is None
            assert checkpoints == []
            assert actions == []
            return

        result = await app.run_dummy(a=a, b=b)
        assert result == f"{a}-{b}"
        assert actions == ["dummy_tool"]
        assert [body["stage"] for body in checkpoints] == ["pre", "post"]
        observed_context = checkpoints[0]["step"]["context"]
        if case == "params-object":
            assert observed_context["note"] == "dynamic-params"
        elif case == "input-object":
            assert observed_context["note"] == "dynamic-input"
        elif case == "kwargs-object":
            assert observed_context["a"] == "x"
            assert observed_context["b"] == "y"
        else:
            assert observed_context["note"] == "static"
    finally:
        await configured.aclose()


async def _configured_receiver_method_probe(
    input_from: dict[str, object] | None,
) -> tuple[object, list[dict[str, object]], object, list[bool]]:
    checks: list[dict[str, object]] = []

    class Tool:
        def __init__(self) -> None:
            self.calls = 0
            self.receiver_identity: list[bool] = []

        async def execute(receiver, item: object) -> dict[str, object]:
            receiver.calls += 1
            receiver.receiver_identity.append(receiver is service)
            return {"calls": receiver.calls, "item": item}

    service = Tool()
    app = SimpleNamespace()

    async def run(item: object) -> object:
        return await service.execute(item)

    app.run = run

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    checkpoint: dict[str, object] = {"target": "service.execute"}
    if input_from is not None:
        checkpoint["input_from"] = input_from
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {"execute": checkpoint},
        },
        agent_name="agent-alpha",
        bindings={"app": app, "service": service},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        result = await app.run({"payload": "user-input"})
    finally:
        await configured.aclose()
    return result, checks, service.calls, service.receiver_identity


@pytest.mark.asyncio
async def test_configured_bound_method_default_input_omits_receiver() -> None:
    item = {"payload": "user-input"}
    result, checks, receiver_calls, receiver_identity = await _configured_receiver_method_probe(
        input_from=None
    )

    assert result == {"calls": 1, "item": item}
    assert receiver_calls == 1
    assert receiver_identity == [True]
    assert [body["stage"] for body in checks] == ["pre", "post"]
    assert checks[0]["step"]["input"] == {"item": item}
    assert checks[1]["step"]["input"] == {"item": item}


@pytest.mark.asyncio
async def test_configured_bound_method_mapped_input_preserves_receiver() -> None:
    item = {"payload": "user-input"}
    result, checks, receiver_calls, receiver_identity = await _configured_receiver_method_probe(
        input_from={"path": ["params", "item"]}
    )

    assert result == {"calls": 1, "item": item}
    assert receiver_calls == 1
    assert receiver_identity == [True]
    assert [body["stage"] for body in checks] == ["pre", "post"]
    assert checks[0]["step"]["input"] == item
    assert checks[1]["step"]["input"] == item


@pytest.mark.asyncio
async def test_configured_receiver_projection_preserves_descriptors_and_plain_self(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[dict[str, object]] = []

    class Service:
        def __init__(self) -> None:
            self.calls = 0
            self.receiver_identity: list[bool] = []

        async def execute(receiver, value: str) -> str:
            receiver.calls += 1
            receiver.receiver_identity.append(receiver is service)
            return value

    class Controls:
        class_calls = 0

        @classmethod
        async def class_execute(klass, value: str) -> str:
            klass.class_calls += 1
            return value

        @staticmethod
        async def static_execute(value: str) -> str:
            return value

    async def plain(self: str, value: str) -> str:
        return f"{self}:{value}"

    service = Service()
    app = SimpleNamespace()

    async def run() -> tuple[str, ...]:
        return (
            await service.execute("instance"),
            await Controls.class_execute("class"),
            await Controls.static_execute("static"),
            await app.plain("plain-self", "plain-value"),
        )

    app.run = run
    app.plain = plain

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {
                "instance": {"target": "service.execute"},
                "class": {"target": "Controls.class_execute"},
                "static": {"target": "Controls.static_execute"},
                "plain": {"target": "app.plain"},
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app, "service": service, "Controls": Controls},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run() == ("instance", "class", "static", "plain-self:plain-value")
        pre_checks = [check for check in checks if check["stage"] == "pre"]
        inputs = {check["step"]["name"]: check["step"]["input"] for check in pre_checks}
        assert inputs == {
            "instance": {"value": "instance"},
            "class": {"value": "class"},
            "static": {"value": "static"},
            "plain": {"self": "plain-self", "value": "plain-value"},
        }
        assert service.calls == 1
        assert service.receiver_identity == [True]
        schemas = {
            schema.name: schema.input_schema
            for schema in configured.schemas["agent-alpha"]
            if schema.name in {"instance", "class", "static", "plain"}
        }
        assert set(schemas["instance"]["properties"]) == {"value"}
        assert set(schemas["class"]["properties"]) == {"value"}
        assert set(schemas["static"]["properties"]) == {"value"}
        assert set(schemas["plain"]["properties"]) == {"self", "value"}
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_state_provider_receives_only_bound_method_user_arguments() -> None:
    checks: list[dict[str, object]] = []
    provider_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Service:
        async def execute(receiver, value: str = "default") -> str:
            return value

    service = Service()
    app = SimpleNamespace()

    async def run() -> str:
        return await service.execute()

    app.run = run

    def snapshot(*args: object, **kwargs: object) -> dict[str, object]:
        provider_calls.append((args, kwargs))
        return {"captured": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    configured = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run"],
            "checkpoints": {"execute": {"target": "service.execute", "state_provider": "snapshot"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app, "service": service},
        providers={"snapshot": snapshot},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run() == "default"
        assert provider_calls == [(("default",), {})]
        assert [check["step"]["input"] for check in checks] == [
            {"value": "default"},
            {"value": "default"},
        ]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_variadic_method_preserves_first_argument_as_receiver() -> None:
    checks: list[dict[str, object]] = []

    class Service:
        def __init__(self) -> None:
            self.identity: list[bool] = []

        async def execute(*values: object) -> str:
            receiver = values[0]
            receiver.identity.append(receiver is service)
            return str(values[1])

    service = Service()
    app = SimpleNamespace()

    async def run() -> str:
        return await service.execute("user-value")

    app.run = run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {"execute": {"target": "service.execute"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app, "service": service},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run() == "user-value"
        assert service.identity == [True]
        assert [check["step"]["input"] for check in checks] == [
            {"values": ["user-value"]},
            {"values": ["user-value"]},
        ]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_bound_method_llm_payload_uses_projected_parameters() -> None:
    checks: list[dict[str, object]] = []

    class Service:
        async def prompt(receiver, text: str) -> str:
            return text

    service = Service()
    app = SimpleNamespace()

    async def run() -> str:
        return await service.prompt("user prompt")

    app.run = run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {"prompt": {"target": "service.prompt", "step_type": "llm"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app, "service": service},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run() == "user prompt"
        assert [check["step"]["input"] for check in checks] == ["user prompt", "user prompt"]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_wrapped_bound_method_alias_schema_omits_receiver() -> None:
    checks: list[dict[str, object]] = []

    class Service:
        async def _execute(receiver, value: str) -> str:
            return value

        @functools.wraps(_execute)
        async def execute(self, value: str) -> str:
            return await Service._execute(self, value)

    service = Service()
    app = SimpleNamespace()
    app.action = service.execute

    async def run() -> str:
        return await app.action("aliased-value")

    app.run = run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {"aliased": {"target": "app.action"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "transport": httpx.MockTransport(handler),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run() == "aliased-value"
        assert [check["step"]["input"] for check in checks] == [
            {"value": "aliased-value"},
            {"value": "aliased-value"},
        ]
        schema = next(
            schema for schema in configured.schemas["agent-alpha"] if schema.name == "aliased"
        )
        assert set(schema.input_schema["properties"]) == {"value"}
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_unknown_parameter_names_are_diagnostic_before_side_effects() -> None:
    requests: list[httpx.Request] = []
    app = SimpleNamespace()
    effects: list[str] = []

    async def action(recipient: str) -> str:
        effects.append(recipient)
        return recipient

    async def run() -> str:
        return await app.action("safe-value")

    app.action = action
    app.run = run
    original_action = app.action

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    connection = {
        "runtime_url": URL,
        "agent_key": "key-alpha",
        "transport": httpx.MockTransport(handler),
        "observability_enabled": False,
        "refresh_interval_seconds": 0,
    }
    invalid_config = {
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {
            "send": {
                "target": "app.action",
                "input_from": {"path": ["params", "recipent"]},
            }
        },
    }
    with pytest.raises(ValueError) as error:
        await norma.aconfigure(
            invalid_config,
            agent_name="agent-alpha",
            bindings={"app": app},
            connection=connection,
        )
    assert "Checkpoint 'send'" in str(error.value)
    assert "/checkpoints/send/input_from" in str(error.value)
    assert "recipent" in str(error.value)
    assert requests == []
    assert app.action is original_action
    assert effects == []

    valid_config = {
        **invalid_config,
        "checkpoints": {
            "send": {
                "target": "app.action",
                "input_from": {"path": ["params", "recipient"]},
                "step_type": "llm",
            }
        },
    }
    configured = await norma.aconfigure(
        valid_config,
        agent_name="agent-alpha",
        bindings={"app": app},
        connection=connection,
    )
    try:
        assert await app.run() == "safe-value"
        assert effects == ["safe-value"]
        assert [
            request.url.path.rsplit("/", 1)[-1]
            for request in requests
            if request.url.path.endswith("/checks")
        ] == [
            "checks",
            "checks",
        ]
    finally:
        await configured.aclose()


@pytest.mark.parametrize(
    "source",
    [
        '{"version":true,"entrypoints":["app.run"],"checkpoints":{}}',
        '{"version":1,"entrypoints":[],"checkpoints":{}}',
        '{"version":1,"entrypoints":["app.run","app.run"],"checkpoints":{}}',
        '{"version":1,"entrypoints":["run"],"checkpoints":{}}',
        '{"version":1,"entrypoints":["app.run"],"unknown":1,"checkpoints":{}}',
        '{"version":1,"entrypoints":["app.run"],"checkpoints":{"send":{"target":"app.send","state":{},"state_provider":"p"}}}',
        '{"version":1,"entrypoints":["app.run"],"checkpoints":{},"checkpoints":{}}',
    ],
)
def test_parser_rejects_invalid_and_duplicate_json(source: str) -> None:
    with pytest.raises(ValueError):
        norma.parse_instrumentation(source)


def test_parser_rejects_duplicate_yaml_keys() -> None:
    with pytest.raises(ValueError, match="Duplicate YAML key"):
        norma.parse_instrumentation(
            "version: 1\nentrypoints: [app.run]\ncheckpoints: {}\ncheckpoints: {}\n", "yaml"
        )


@pytest.mark.parametrize("configure_name", ["configure", "aconfigure"])
def test_configure_rejects_all_params_before_target_resolution_or_network(
    monkeypatch: pytest.MonkeyPatch, configure_name: str
) -> None:
    source = {
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {"send": {"target": "app.missing", "input_from": "all_params"}},
    }

    def unexpected_client(*args: object, **kwargs: object) -> NormaClient:
        raise AssertionError("unsupported input source must fail before network setup")

    monkeypatch.setattr("norma.instrumentation.NormaClient", unexpected_client)
    monkeypatch.setattr("norma.checkpoints.NormaClient", unexpected_client)
    configure_fn = getattr(norma, configure_name)
    kwargs = {
        "agent_name": "agent-alpha",
        "bindings": {"app": SimpleNamespace()},
        "connection": {
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    }

    if configure_name == "configure":
        with pytest.raises(ValueError, match=r"input_from.*all_params|all_params.*input_from"):
            configure_fn(source, **kwargs)
        return

    async def exercise() -> None:
        with pytest.raises(ValueError, match=r"input_from.*all_params|all_params.*input_from"):
            await configure_fn(source, **kwargs)

    asyncio.run(exercise())


def test_parser_accepts_custom_checkpoint_step_type() -> None:
    parsed = norma.parse_instrumentation(
        '{"version":1,"entrypoints":["app.run"],'
        '"checkpoints":{"custom":{"target":"app.action","step_type":"workflow"}}}'
    )
    assert parsed["checkpoints"]["custom"]["step_type"] == "workflow"


def test_v2_parser_accepts_additive_action_state_and_connection_sources() -> None:
    config = norma.parse_instrumentation(
        json.dumps(
            {
                "version": 2,
                "entrypoints": ["app.run"],
                "connection": {
                    "runtime_url": {"env": "NORMA_RUNTIME_URL"},
                    "agent_key": {"binding": "credentials.agent_key"},
                    "timeout_ms": 2500,
                    "target_type": "tenant",
                    "target_id": "tenant-7",
                    "control_contract": "rule-controls-v1",
                },
                "telemetry": {"enabled": False},
                "refresh": {"interval_seconds": 0},
                "checkpoints": {
                    "edit": {
                        "target": "app.action",
                        "step_type": "tool",
                        "options": "native_edit_options",
                        "state_from": {"path": ["kwargs", "state"]},
                        "action": {
                            "kind": "edit",
                            "target": {"path": ["kwargs", "target"]},
                            "command": {"path": ["kwargs", "command"]},
                            "cwd": {"path": ["kwargs", "cwd"]},
                        },
                    }
                },
            }
        )
    )
    assert config["version"] == 2
    assert config["checkpoints"]["edit"]["state_from"] == {"path": ["kwargs", "state"]}
    assert config["checkpoints"]["edit"]["action"]["kind"] == "edit"
    assert config["connection"]["timeout_ms"] == 2500


def test_invalid_config_diagnostic_never_echoes_other_configuration_values() -> None:
    secret_marker = "SECRET_MARKER_DO_NOT_ECHO"
    source = json.dumps(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {},
            "connection": {"agent_key": secret_marker},
            "telemetry": {"batch_size": -1},
        }
    )

    with pytest.raises(ValueError) as error:
        norma.parse_instrumentation(source)

    assert "connection.agent_key" in str(error.value)
    assert secret_marker not in str(error.value)


def test_invalid_settings_diagnostic_never_echoes_credentials() -> None:
    secret_marker = "SECRET_KEY_DO_NOT_ECHO"
    with pytest.raises(ValueError) as error:
        norma.configure(
            {
                "version": 2,
                "entrypoints": ["app.run"],
                "checkpoints": {},
            },
            agent_name="agent-alpha",
            bindings={"app": SimpleNamespace(run=lambda: None)},
            connection={
                "runtime_url": URL,
                "agent_key": secret_marker,
                "max_retries": -1,
            },
        )

    assert "max_retries" in str(error.value)
    assert secret_marker not in str(error.value)


@pytest.mark.asyncio
async def test_v2_explicit_schema_is_reported_and_only_published_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    app.run = lambda: "ok"
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.method == "PUT" and request.url.path.endswith("/agents/agent-alpha/steps"):
            assert body is not None
            return httpx.Response(200, json={"steps": body["steps"]})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    schema = {
        "type": "tool",
        "name": "reviewed_tool",
        "description": "Reviewed existing tool schema",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "string"},
        "metadata": {"reviewed": True},
    }
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {},
            "schemas": {"publish": True, "steps": [schema]},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        assert [
            item.model_dump(mode="json", exclude_none=True)
            for item in configured.schemas["agent-alpha"]
        ] == [schema]
        assert [(method, path.rsplit("/api/v1", 1)[-1]) for method, path, _ in requests] == [
            ("GET", "/agents/agent-alpha"),
            ("GET", "/agents/agent-alpha/rules"),
            ("PUT", "/agents/agent-alpha/steps"),
        ]
        assert requests[-1][2] == {"steps": [schema]}
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_entrypoint_input_and_params_bind_positional_keyword_and_default_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_request = {"turn_id": "default-turn"}
    app = SimpleNamespace()
    turn_records: list[dict[str, object]] = []

    async def run(
        request: dict[str, object] = default_request,
        *,
        tenant: str = "default-tenant",
        **metadata: str,
    ) -> str:
        original_turn_id = str(request["turn_id"])
        if request is default_request:
            request["turn_id"] = "mutated-after-entry"
        return f"finished-{original_turn_id}"

    app.run = run

    def acknowledged(payload: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"control_contract": "rule-controls-v1", **payload},
            headers={"X-Norma-Control-Contract": "rule-controls-v1"},
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return acknowledged({"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return acknowledged({"rules": []})
        if request.url.path.endswith("/checks"):
            turn_records.append(json.loads(request.content)["step"]["input"])
            return acknowledged(
                {
                    "execution_contract": "hosted-v1",
                    "verdict": "pass",
                    "reason": "Complete",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": {"count": 0, "max": 2},
                    "allow_finish": True,
                    "outcome": "completed",
                }
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.finish.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "app.run",
                    "turn": {
                        "session_id": {"path": ["params", "tenant"]},
                        "turn_id": {"path": ["input", "turn_id"]},
                        "user_request": {"path": ["params", "metadata", "request_id"]},
                        "finish": {
                            "final_message": {"path": ["input", "turn_id"]},
                            "on_hold": "raise",
                        },
                    },
                }
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        positional = {"turn_id": "positional-turn"}
        keyword = {"turn_id": "keyword-turn"}
        assert await app.run(positional, tenant="positional-team", request_id="positional") == (
            "finished-positional-turn"
        )
        assert (
            await app.run(request=keyword, tenant="keyword-team", request_id="keyword")
            == "finished-keyword-turn"
        )
        assert await app.run(tenant="default-team", request_id="default") == (
            "finished-default-turn"
        )

        assert [record["session_id"] for record in turn_records] == [
            "positional-team",
            "keyword-team",
            "default-team",
        ]
        assert [record["turn_id"] for record in turn_records] == [
            "positional-turn",
            "keyword-turn",
            "default-turn",
        ]
        assert [record["user_request"] for record in turn_records] == [
            "positional",
            "keyword",
            "default",
        ]
        assert [record["final_message"] for record in turn_records] == [
            "positional-turn",
            "keyword-turn",
            "default-turn",
        ]
        assert default_request["turn_id"] == "mutated-after-entry"
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_entrypoint_named_selectors_cover_variadic_and_unicode_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    turns: list[dict[str, object]] = []

    async def varargs(*values: object) -> str:
        return "varargs-result"

    async def varkw(**options: str) -> str:
        return "varkw-result"

    async def unicode_parameter(élément: dict[str, str]) -> str:
        return "unicode-result"

    app.varargs = varargs
    app.varkw = varkw
    app.unicode_parameter = unicode_parameter

    def acknowledged(payload: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"control_contract": "rule-controls-v1", **payload},
            headers={"X-Norma-Control-Contract": "rule-controls-v1"},
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return acknowledged({"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return acknowledged({"rules": []})
        if request.url.path.endswith("/checks"):
            body = json.loads(request.content)
            turns.append(body["step"]["input"])
            return acknowledged(
                {
                    "execution_contract": "hosted-v1",
                    "verdict": "pass",
                    "reason": "Complete",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": {"count": 0, "max": 2},
                    "allow_finish": True,
                    "outcome": "completed",
                }
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.finish.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "app.varargs",
                    "turn": {
                        "session_id": {"value": "varargs-session"},
                        "turn_id": {"path": ["input", 0]},
                        "finish": {
                            "final_message": {"path": ["params", "values", 1, "message"]},
                            "on_hold": "raise",
                        },
                    },
                },
                {
                    "target": "app.varkw",
                    "turn": {
                        "session_id": {"value": "varkw-session"},
                        "turn_id": {"path": ["input", "turn_id"]},
                        "finish": {
                            "final_message": {"path": ["params", "options", "message"]},
                            "on_hold": "raise",
                        },
                    },
                },
                {
                    "target": "app.unicode_parameter",
                    "turn": {
                        "session_id": {"value": "unicode-session"},
                        "turn_id": {"path": ["params", "élément", "turn_id"]},
                        "finish": {
                            "final_message": {"path": ["input", "message"]},
                            "on_hold": "raise",
                        },
                    },
                },
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.varargs("varargs-turn", {"message": "varargs-final"}) == ("varargs-result")
        assert await app.varkw(turn_id="varkw-turn", message="varkw-final") == "varkw-result"
        unicode_request = {"turn_id": "unicode-turn", "message": "unicode-final"}
        assert await app.unicode_parameter(élément=unicode_request) == "unicode-result"
        assert [(turn["turn_id"], turn["final_message"]) for turn in turns] == [
            ("varargs-turn", "varargs-final"),
            ("varkw-turn", "varkw-final"),
            ("unicode-turn", "unicode-final"),
        ]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "message"),
    [
        ("entrypoint", "TypeScript-only"),
        ("checkpoint", "TypeScript-only"),
        ("operation", "TypeScript-only"),
        ("unknown-parameter", "no target parameter 'missing'"),
        ("params-without-name", "turn_id.path"),
    ],
)
async def test_python_rejects_unsupported_or_invalid_named_selectors_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    message: str,
) -> None:
    app = SimpleNamespace()
    requests: list[httpx.Request] = []

    async def target(payload: dict[str, object] | None = None) -> str:
        return "result"

    app.target = target
    app.run = target
    config: dict[str, object] = {
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {},
    }
    if scope == "entrypoint":
        config["entrypoints"] = [{"target": "app.run", "parameter_names": ["payload"]}]
    elif scope == "checkpoint":
        config["checkpoints"] = {"target": {"target": "app.target", "parameter_names": ["payload"]}}
    elif scope == "operation":
        config["operations"] = {
            "target": {
                "target": "app.target",
                "parameter_names": ["payload"],
                "operation_key": {"value": "operation"},
            }
        }
    elif scope == "unknown-parameter":
        config["entrypoints"] = [
            {
                "target": "app.run",
                "turn": {
                    "session_id": {"value": "session"},
                    "turn_id": {"path": ["params", "missing"]},
                },
            }
        ]
    else:
        config["entrypoints"] = [
            {
                "target": "app.run",
                "turn": {
                    "session_id": {"value": "session"},
                    "turn_id": {"path": ["params"]},
                },
            }
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    with pytest.raises(ValueError, match=message):
        await norma.aconfigure(
            config,
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={"runtime_url": URL, "agent_key": "key-alpha"},
        )
    assert requests == []
    assert app.run is target
    assert app.target is target


@pytest.mark.asyncio
async def test_new_named_selectors_require_inspectable_signature_but_legacy_targets_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()

    async def run(request: dict[str, str]) -> str:
        return request["message"]

    run.__signature__ = object()  # type: ignore[attr-defined]
    app.run = run
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    legacy = await norma.aconfigure(
        {"version": 2, "entrypoints": ["app.run"], "checkpoints": {}},
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha"},
    )
    await legacy.aclose()
    requests.clear()

    with pytest.raises(ValueError, match="inspectable target signature"):
        await norma.aconfigure(
            {
                "version": 2,
                "entrypoints": [
                    {
                        "target": "app.run",
                        "turn": {
                            "session_id": {"value": "session"},
                            "turn_id": {"path": ["input", "turn_id"]},
                        },
                    }
                ],
                "checkpoints": {},
            },
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={"runtime_url": URL, "agent_key": "key-alpha"},
        )
    assert requests == []
    assert app.run is run


@pytest.mark.asyncio
async def test_entrypoint_named_selectors_skip_receiver_for_decorated_bound_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def transparent(function: object) -> object:
        @functools.wraps(function)
        async def wrapper(*args: object, **kwargs: object) -> object:
            return await function(*args, **kwargs)  # type: ignore[operator]

        return wrapper

    class Service:
        @transparent
        async def run(
            self,
            request: dict[str, object],
            /,
            *,
            tenant: str = "method-default",
        ) -> str:
            return str(request["turn_id"])

        @transparent
        async def run_variadic(*values: str) -> str:
            return values[2]

    app = SimpleNamespace(service=Service())
    turn_records: list[dict[str, object]] = []

    def acknowledged(payload: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"control_contract": "rule-controls-v1", **payload},
            headers={"X-Norma-Control-Contract": "rule-controls-v1"},
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return acknowledged({"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return acknowledged({"rules": []})
        if request.url.path.endswith("/checks"):
            turn_records.append(json.loads(request.content)["step"]["input"])
            return acknowledged(
                {
                    "execution_contract": "hosted-v1",
                    "verdict": "pass",
                    "reason": "Complete",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": {"count": 0, "max": 2},
                    "allow_finish": True,
                    "outcome": "completed",
                }
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.finish.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "app.service.run",
                    "turn": {
                        "session_id": {"path": ["params", "tenant"]},
                        "turn_id": {"path": ["input", "turn_id"]},
                        "finish": {
                            "final_message": {"path": ["result"]},
                            "on_hold": "raise",
                        },
                    },
                },
                {
                    "target": "app.service.run_variadic",
                    "turn": {
                        "session_id": {"value": "variadic-session"},
                        "turn_id": {"path": ["input", 0]},
                        "finish": {
                            "final_message": {"path": ["params", "values", 1]},
                            "on_hold": "raise",
                        },
                    },
                },
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.service.run({"turn_id": "method-turn"}) == "method-turn"
        assert await app.service.run_variadic("variadic-turn", "variadic-final") == (
            "variadic-final"
        )
        assert turn_records[0]["session_id"] == "method-default"
        assert turn_records[0]["turn_id"] == "method-turn"
        assert turn_records[1]["turn_id"] == "variadic-turn"
        assert turn_records[1]["final_message"] == "variadic-final"
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_checkpoint_input_selector_preserves_mutable_default_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    default_payload = {"items": ["before"]}
    checks: list[dict[str, object]] = []

    async def mutate(payload: dict[str, object] = default_payload) -> str:
        items = payload["items"]
        assert isinstance(items, list)
        items.append("during-action")
        return "done"

    async def run() -> str:
        return await app.mutate()

    app.mutate, app.run = mutate, run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {
                "mutate": {
                    "target": "app.mutate",
                    "input_from": {"path": ["input"]},
                    "output_from": {"path": ["params", "payload", "items"]},
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run() == "done"
        assert [check["stage"] for check in checks] == ["pre", "post"]
        assert all(check["step"]["input"] == {"items": ["before"]} for check in checks)
        assert checks[0]["step"]["output"] is None
        assert checks[1]["step"]["output"] == ["before"]
        assert default_payload == {"items": ["before"]}
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_checkpoint_all_kwargs_input_copies_explicit_kwargs_and_reuses_precheck_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    checks: list[dict[str, object]] = []

    async def mutate(
        positional: str,
        *,
        payload: dict[str, object],
        flag: str = "omitted-default",
    ) -> str:
        payload["items"] = ["after"]
        return "done"

    async def run() -> str:
        return await app.mutate("positional-value", payload={"items": ["before"]})

    app.mutate, app.run = mutate, run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {"mutate": {"target": "app.mutate", "input_from": "all_kwargs"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run() == "done"
        assert [check["stage"] for check in checks] == ["pre", "post"]
        assert all(check["step"]["input"] == {"payload": {"items": ["before"]}} for check in checks)
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_checkpoint_named_output_reuses_snapshot_across_precheck_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    default_payload = {"items": ["before"]}
    checks: list[dict[str, object]] = []

    async def mutate(payload: dict[str, object] = default_payload) -> str:
        return str(payload["items"][-1])  # type: ignore[index]

    async def run() -> str:
        return await app.mutate()

    app.mutate, app.run = mutate, run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            body = json.loads(request.content)
            checks.append(body)
            if body["stage"] == "pre":
                default_payload["items"].append("changed-during-precheck")  # type: ignore[union-attr]
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {
                "mutate": {
                    "target": "app.mutate",
                    "input_from": {"path": ["input"]},
                    "output_from": {"path": ["params", "payload", "items"]},
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await app.run() == "before"
        assert [check["stage"] for check in checks] == ["pre", "post"]
        assert checks[0]["step"]["input"] == {"items": ["before"]}
        assert checks[1]["step"]["input"] == {"items": ["before"]}
        assert checks[1]["step"]["output"] == ["before"]
        assert default_payload == {"items": ["before", "changed-during-precheck"]}
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_operation_post_observation_uses_initial_named_default_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    default_payload = {"resource": "before"}
    proposal: dict[str, object] | None = None
    observations: list[dict[str, object]] = []

    def commit(
        payload: dict[str, object] = default_payload,
        *,
        key: str = "default-operation",
    ) -> str:
        payload["resource"] = "after"
        return key

    def run() -> str:
        return app.commit()

    app.commit, app.run = commit, run

    def operation_record(items: list[dict[str, object]]) -> dict[str, object]:
        assert proposal is not None
        return {
            "id": "operation-default",
            "operation_key": "default-operation",
            "agent_name": "agent-alpha",
            "created_at": "2026-10-30T00:00:00Z",
            "request_fingerprint": "fingerprint",
            "request": proposal,
            "decision": {
                "outcome": "allow",
                "reason": "allowed",
                "check": {
                    "allowed": True,
                    "confidence": 1,
                    "execution_contract": "hosted-v1",
                },
            },
            "observations": items,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal proposal
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.method == "POST" and request.url.path.endswith("/operations"):
            proposal = json.loads(request.content)
            return httpx.Response(200, json=operation_record([]))
        if request.method == "POST" and request.url.path.endswith(
            "/operations/operation-default/observations"
        ):
            body = json.loads(request.content)
            observations.append(body)
            item = {
                "id": f"observation-{len(observations)}",
                "event_key": body["event_key"],
                "kind": body["kind"],
                "summary": body["summary"],
                "external_reference": None,
                "recorded_at": "2026-10-30T00:00:01Z",
                "source": "caller_reported",
            }
            return httpx.Response(200, json=operation_record([item]))
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    for module in (
        "norma.instrumentation",
        "norma._instrumentation_operations",
        "norma.client",
    ):
        monkeypatch.setattr(f"{module}.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {},
            "operations": {
                "commit": {
                    "target": "app.commit",
                    "operation_key": {"path": ["params", "key"]},
                    "input": {"path": ["input"]},
                    "observations": {
                        "succeeded": {
                            "event_key": {"value": "commit-succeeded"},
                            "summary": {"path": ["params", "payload", "resource"]},
                        }
                    },
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha"},
    )
    try:
        assert app.run() == "default-operation"
        assert proposal is not None
        assert proposal["operation_key"] == "default-operation"
        assert proposal["step"]["input"] == {"resource": "before"}
        assert observations[0]["summary"] == "before"
        assert default_payload == {"resource": "after"}
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_schema_expectations_validate_existing_steps_before_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(run=lambda: "ok")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method + " " + request.url.path)
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(
                200,
                json={
                    "agent": {"agent_name": "agent-alpha"},
                    "steps": [
                        {
                            "type": "tool",
                            "name": "reviewed_tool",
                            "input_schema": {"type": "string"},
                        }
                    ],
                },
            )
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    with pytest.raises(ValueError, match="does not match existing"):
        await norma.aconfigure(
            {
                "version": 2,
                "entrypoints": ["app.run"],
                "checkpoints": {},
                "schemas": {
                    "steps": [
                        {
                            "type": "tool",
                            "name": "reviewed_tool",
                            "input_schema": {"type": "object"},
                        }
                    ]
                },
            },
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={
                "runtime_url": URL,
                "agent_key": "key-alpha",
                "observability_enabled": False,
            },
        )
    assert [item.split(" ", 1)[0] for item in requests] == ["GET", "GET"]
    assert all("/steps" not in item for item in requests)


@pytest.mark.asyncio
async def test_missing_schema_expectation_requires_explicit_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(run=lambda: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    with pytest.raises(ValueError, match="missing tool:expected"):
        await norma.aconfigure(
            {
                "version": 2,
                "entrypoints": ["app.run"],
                "checkpoints": {},
                "schemas": {
                    "steps": [
                        {"type": "tool", "name": "expected", "input_schema": {"type": "object"}}
                    ]
                },
            },
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={
                "runtime_url": URL,
                "agent_key": "key-alpha",
                "observability_enabled": False,
            },
        )


@pytest.mark.asyncio
async def test_schema_publication_preserves_explicit_nulls_and_omits_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(run=lambda: None)
    publications: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.method == "PUT" and request.url.path.endswith("/steps"):
            body = json.loads(request.content)
            publications.extend(body["steps"])
            return httpx.Response(200, json={"steps": body["steps"]})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {},
            "schemas": {
                "publish": True,
                "steps": [
                    {"type": "tool", "name": "clear_fields", "description": None, "metadata": None},
                    {"type": "tool", "name": "defaults_omitted"},
                ],
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        clear, omitted = publications
        assert "description" in clear and clear["description"] is None
        assert "metadata" in clear and clear["metadata"] is None
        assert "description" not in omitted
        assert "metadata" not in omitted
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_multi_agent_schema_conflicts_prevent_every_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = SimpleNamespace(run_alpha=lambda: None, run_bravo=lambda: None)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method + " " + request.url.path)
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if "/agents/" in request.url.path and request.method == "GET":
            name = request.url.path.split("/agents/", 1)[1].split("/", 1)[0]
            existing = (
                [{"type": "tool", "name": "shared", "input_schema": {"type": "array"}}]
                if name == "agent-alpha"
                else []
            )
            return httpx.Response(200, json={"agent": {"agent_name": name}, "steps": existing})
        if request.method == "PUT":
            return httpx.Response(200, json={"steps": json.loads(request.content)["steps"]})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    configs = {
        "agent-alpha": {
            "version": 2,
            "entrypoints": ["app.run_alpha"],
            "checkpoints": {},
            "schemas": {
                "publish": True,
                "steps": [{"type": "tool", "name": "shared", "input_schema": {"type": "string"}}],
            },
        },
        "agent-bravo": {
            "version": 2,
            "entrypoints": ["app.run_bravo"],
            "checkpoints": {},
            "schemas": {
                "publish": True,
                "steps": [{"type": "tool", "name": "shared", "input_schema": {"type": "object"}}],
            },
        },
    }
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    for name, config in configs.items():
        (config_dir / f"{name}.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match existing"):
        await norma.aconfigure(
            config_dir,
            bindings={"app": app},
            connections={
                "agent-alpha": {
                    "runtime_url": URL,
                    "agent_key": "key-alpha",
                    "observability_enabled": False,
                },
                "agent-bravo": {
                    "runtime_url": URL,
                    "agent_key": "key-bravo",
                    "observability_enabled": False,
                },
            },
        )
    assert not any("PUT" in item for item in requests)


@pytest.mark.asyncio
async def test_close_during_async_entrypoint_resolution_blocks_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    started, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    async def context_provider(invocation: dict[str, object]) -> dict[str, str]:
        del invocation
        started.set()
        await release.wait()
        return {"tenant": "alpha"}

    async def run() -> str:
        effects.append("ran")
        return "ok"

    app.context_provider, app.run = context_provider, run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "app.run",
                    "context": {"provider": "app.context_provider"},
                }
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    pending = asyncio.create_task(app.run())
    await started.wait()
    await configured.aclose()
    release.set()
    with pytest.raises(norma.CheckError, match="session changed or closed"):
        await pending
    assert effects == []


@pytest.mark.asyncio
async def test_close_in_on_verdict_blocks_configured_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    calls: list[str] = []
    configured_handle: norma.ConfiguredAgents | None = None

    async def run() -> str:
        calls.append("run")
        return "draft"

    def on_verdict(payload: dict[str, object]) -> None:
        assert payload["verdict"].verdict == "hold"
        assert configured_handle is not None
        configured_handle.close()

    def continue_turn(payload: dict[str, object]) -> str:
        del payload
        calls.append("continue")
        return "revised"

    app.run, app.on_verdict, app.continue_turn = run, on_verdict, continue_turn

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            return httpx.Response(
                200,
                json={
                    "execution_contract": "hosted-v1",
                    "verdict": "hold",
                    "reason": "Revise",
                    "reason_codes": ["claim_contradicted"],
                    "held_by": [3],
                    "would_hold_by": [],
                    "continuation": {"count": 0, "max": 1},
                    "allow_finish": False,
                    "outcome": "held",
                },
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.finish.NormaClient", client_factory)
    configured_handle = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "app.run",
                    "turn": {
                        "session_id": {"value": "session-a"},
                        "turn_id": {"value": "turn-a"},
                        "finish": {
                            "driver": "generic",
                            "final_message": {"value": "draft"},
                            "on_hold": "raise",
                            "continue_with": {"binding": "app.continue_turn"},
                            "on_verdict": {"binding": "app.on_verdict"},
                        },
                    },
                }
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    with pytest.raises(norma.CheckError, match="session changed or closed"):
        await app.run()
    assert calls == ["run"]


@pytest.mark.asyncio
async def test_publication_revalidates_targets_before_installing_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    app.run = lambda: "run"

    def original() -> str:
        return "original"

    def replacement() -> str:
        return "application replacement"

    app.action = original
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method + " " + request.url.path)
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.method == "PUT" and request.url.path.endswith("/agents/agent-alpha/steps"):
            app.action = replacement
            return httpx.Response(200, json={"steps": json.loads(request.content)["steps"]})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    config = {
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {"action": {"target": "app.action"}},
        "schemas": {"publish": True},
    }

    with pytest.raises(ValueError, match="changed during configuration"):
        await norma.aconfigure(
            config,
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={"runtime_url": URL, "agent_key": "key-alpha"},
        )

    assert app.action is replacement
    assert any("/steps" in request for request in requests)


@pytest.mark.parametrize(
    ("file_connection", "message"),
    [
        ({"agent_key": {"env": "NORMA_MISSING_TEST_KEY"}}, "NORMA_MISSING_TEST_KEY"),
        ({"http_client": {"binding": "app.client"}}, "does not support"),
    ],
)
@pytest.mark.asyncio
async def test_v2_connection_preflight_is_safe_and_performs_no_auth_get(
    file_connection: dict[str, object],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NORMA_MISSING_TEST_KEY", raising=False)
    app = SimpleNamespace()

    def original() -> str:
        return "original"

    app.run = original
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method + " " + request.url.path)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    with pytest.raises(ValueError) as exc_info:
        await norma.aconfigure(
            {
                "version": 2,
                "connection": file_connection,
                "entrypoints": ["app.run"],
                "checkpoints": {},
            },
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={"runtime_url": URL, "observability_enabled": False},
        )
    assert message in str(exc_info.value)
    assert "SECRET_MARKER" not in str(exc_info.value)
    assert app.run is original
    assert requests == []


@pytest.mark.asyncio
async def test_v2_generic_turn_finish_uses_recorder_and_continuation_without_rerunning_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    calls: list[str] = []
    verdict_callbacks: list[dict[str, object]] = []
    turn_requests: list[dict[str, object]] = []
    control_headers: list[str | None] = []

    async def run() -> str:
        calls.append("run")
        return "draft"

    def continue_turn(payload: dict[str, object]) -> str:
        calls.append("continue")
        assert payload["continuation_count"] == 1
        assert payload["result"] == "draft"
        assert "invocation" not in payload
        assert payload["agent_name"] == "agent-alpha"
        return "revised"

    def on_verdict(payload: dict[str, object]) -> None:
        assert "invocation" not in payload
        assert "result" in payload and "turn" in payload and "verdict" in payload
        verdict_callbacks.append(payload)

    app.run, app.continue_turn, app.on_verdict = run, continue_turn, on_verdict

    def acknowledged(payload: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"control_contract": "rule-controls-v1", **payload},
            headers={"X-Norma-Control-Contract": "rule-controls-v1"},
        )

    def handler(request: httpx.Request) -> httpx.Response:
        control_headers.append(request.headers.get("X-Norma-Control-Contract"))
        if request.url.path.endswith("/agents/agent-alpha"):
            return acknowledged({"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return acknowledged({"rules": []})
        if request.url.path.endswith("/checks"):
            body = json.loads(request.content)
            turn_requests.append(body)
            count = len(turn_requests) - 1
            verdict = (
                {
                    "verdict": "hold",
                    "reason": "Revise",
                    "reason_codes": ["claim_contradicted"],
                    "held_by": [3],
                    "would_hold_by": [],
                    "continuation": {"count": 0, "max": 1},
                    "allow_finish": False,
                    "outcome": "held",
                }
                if count == 0
                else {
                    "verdict": "pass",
                    "reason": "Complete",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": {"count": 1, "max": 1},
                    "allow_finish": True,
                    "outcome": "completed",
                }
            )
            generation = "generic-generation-1"
            verdict["continuation"]["agent_generation"] = generation
            return acknowledged(
                {
                    "execution_contract": "hosted-v1",
                    "agent_generation": generation,
                    "control_resolutions": None,
                    "control_evidence": [
                        {
                            "schema_version": 1,
                            "completeness": "unavailable",
                            "agent_name": "agent-alpha",
                            "agent_generation": generation,
                            "rule_id": 17,
                            "rule_version": 2,
                            "definition_fingerprint": "a" * 64,
                            "references": [],
                        }
                    ],
                    **verdict,
                }
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.finish.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "app.run",
                    "turn": {
                        "session_id": {"value": "session-a"},
                        "turn_id": {"value": "turn-a"},
                        "user_request": {"value": "Do the work"},
                        "application": "norma-agent-runtime",
                        "application_version": "3.2",
                        "platform": "norma-agent-runtime",
                        "platform_version": "3.2",
                        "max_continuations": 1,
                        "finish": {
                            "driver": "generic",
                            "final_message": {"path": ["result"]},
                            "on_hold": "raise",
                            "continue_with": {"binding": "app.continue_turn"},
                            "on_verdict": {"binding": "app.on_verdict"},
                        },
                    },
                }
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "control_contract": "rule-controls",
            "observability_enabled": False,
        },
    )
    try:
        assert await app.run() == "revised"
        assert calls == ["run", "continue"]
        assert [body["step"]["input"]["final_message"] for body in turn_requests] == [
            "draft",
            "revised",
        ]
        assert [body["step"]["input"]["continuation"]["count"] for body in turn_requests] == [
            0,
            1,
        ]
        assert all(
            body["step"]["input"]["platform"] == "norma-agent-runtime"
            and body["step"]["input"]["platform_version"] == "3.2"
            for body in turn_requests
        )
        assert control_headers == [None, "rule-controls-v1", "rule-controls-v1", "rule-controls-v1"]
        assert turn_requests[0]["step"]["input"]["continuation"] == {
            "count": 0,
            "max": 1,
        }
        assert turn_requests[1]["step"]["input"]["continuation"] == {
            "count": 1,
            "max": 1,
            "agent_generation": "generic-generation-1",
        }
        assert verdict_callbacks[-1]["verdict"].control_evidence[0].rule_id == 17
        assert len(verdict_callbacks) == 2
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_concurrent_generic_finish_runs_keep_agent_generations_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_by_agent = {
        "agent-alpha": "alpha-generation",
        "agent-bravo": "bravo-generation",
    }
    first_agents: set[str] = set()
    both_first_checks = asyncio.Event()
    turn_requests: list[tuple[str, int, str | None]] = []

    class InterleavingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path
            agent_name = path.rsplit("/", maxsplit=1)[-1]
            if request.method == "GET" and path.endswith("/agents/agent-alpha"):
                return httpx.Response(
                    200,
                    json={"agent": {"agent_name": "agent-alpha"}},
                    request=request,
                )
            if request.method == "GET" and path.endswith("/agents/agent-bravo"):
                return httpx.Response(
                    200,
                    json={"agent": {"agent_name": "agent-bravo"}},
                    request=request,
                )
            if request.method == "GET" and path.endswith("/rules"):
                return httpx.Response(200, json={"rules": []}, request=request)
            if request.method == "POST" and path.endswith("/checks"):
                body = json.loads(request.content)
                agent_name = body["agent_name"]
                continuation = body["step"]["input"]["continuation"]
                count = continuation["count"]
                generation = generation_by_agent[agent_name]
                turn_requests.append((agent_name, count, continuation.get("agent_generation")))
                if count == 0:
                    first_agents.add(agent_name)
                    if len(first_agents) == 2:
                        both_first_checks.set()
                    await asyncio.wait_for(both_first_checks.wait(), timeout=2)
                    kind = "hold"
                else:
                    await asyncio.sleep(0)
                    kind = "pass"
                verdict = {
                    "execution_contract": "hosted-v1",
                    "agent_generation": generation,
                    "verdict": kind,
                    "reason": "Revise" if kind == "hold" else "Complete",
                    "reason_codes": ["claim_contradicted"] if kind == "hold" else [],
                    "held_by": [17] if kind == "hold" else [],
                    "would_hold_by": [],
                    "continuation": {
                        **continuation,
                        "agent_generation": generation,
                    },
                    "allow_finish": kind != "hold",
                    "outcome": "held" if kind == "hold" else "completed",
                }
                return httpx.Response(200, json=verdict, request=request)
            return httpx.Response(404, request=request)

        async def aclose(self) -> None:
            return None

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=InterleavingTransport(),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.finish.NormaClient", client_factory)
    app = SimpleNamespace()

    async def run_alpha() -> str:
        return "alpha draft"

    async def run_bravo() -> str:
        return "bravo draft"

    def continue_alpha(_: dict[str, object]) -> str:
        return "alpha revised"

    def continue_bravo(_: dict[str, object]) -> str:
        return "bravo revised"

    app.run_alpha = run_alpha
    app.run_bravo = run_bravo
    app.continue_alpha = continue_alpha
    app.continue_bravo = continue_bravo

    def profile(target: str, continuation: str, agent_name: str) -> dict[str, object]:
        return {
            "version": 2,
            "entrypoints": [
                {
                    "target": target,
                    "turn": {
                        "session_id": {"value": f"{agent_name}-session"},
                        "turn_id": {"value": f"{agent_name}-turn"},
                        "max_continuations": 1,
                        "finish": {
                            "driver": "generic",
                            "final_message": {"path": ["result"]},
                            "on_hold": "raise",
                            "continue_with": {"binding": continuation},
                        },
                    },
                }
            ],
            "checkpoints": {},
        }

    (tmp_path / "agent-alpha.json").write_text(
        json.dumps(profile("app.run_alpha", "app.continue_alpha", "agent-alpha")),
        encoding="utf-8",
    )
    (tmp_path / "agent-bravo.json").write_text(
        json.dumps(profile("app.run_bravo", "app.continue_bravo", "agent-bravo")),
        encoding="utf-8",
    )
    configured = await norma.aconfigure(
        tmp_path,
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
        connections={
            "agent-alpha": {"agent_key": "key-alpha"},
            "agent-bravo": {"agent_key": "key-bravo"},
        },
    )
    try:
        alpha, bravo = await asyncio.gather(app.run_alpha(), app.run_bravo())
        assert alpha == "alpha revised"
        assert bravo == "bravo revised"
        assert sorted(turn_requests) == [
            ("agent-alpha", 0, None),
            ("agent-alpha", 1, "alpha-generation"),
            ("agent-bravo", 0, None),
            ("agent-bravo", 1, "bravo-generation"),
        ]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_generic_finish_selects_plain_message_from_native_result_without_deepcopy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    app = SimpleNamespace()
    native_result = {"message": "safe result", "client": threading.Lock()}
    captured_turns: list[dict[str, object]] = []

    async def run() -> dict[str, object]:
        return native_result

    app.run = run
    app.on_verdict = lambda _payload: None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            body = json.loads(request.content)
            captured_turns.append(body["step"]["input"])
            return httpx.Response(
                200,
                json={
                    "execution_contract": "hosted-v1",
                    "verdict": "pass",
                    "reason": "Complete",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": {"count": 0, "max": 0},
                    "allow_finish": True,
                    "outcome": "completed",
                },
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.finish.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "app.run",
                    "turn": {
                        "session_id": {"value": "session-a"},
                        "turn_id": {"value": "turn-a"},
                        "finish": {
                            "final_message": {"path": ["result", "message"]},
                            "on_hold": "return",
                            "on_verdict": {"binding": "app.on_verdict"},
                        },
                    },
                }
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        assert await app.run() is native_result
        assert captured_turns[0]["final_message"] == "safe result"
    finally:
        await configured.aclose()


@pytest.mark.parametrize(
    "entrypoint",
    ["app.run", {"target": "app.run"}],
)
@pytest.mark.asyncio
async def test_entrypoints_without_value_selectors_accept_uncopyable_arguments(
    entrypoint: str | dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Uncopyable:
        def __deepcopy__(self, memo: dict[int, object]) -> object:
            raise AssertionError("entrypoint should not copy unobserved arguments")

    app = SimpleNamespace()
    app.run = lambda value: value

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2 if isinstance(entrypoint, dict) else 1,
            "entrypoints": [entrypoint],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    value = Uncopyable()
    try:
        assert app.run(value) is value
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_entrypoint_selector_copies_selected_json_without_copying_native_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    app = SimpleNamespace()
    request = {"context": {"tenant": "tenant-a"}, "client": threading.Lock()}
    observed: list[dict[str, object]] = []

    async def run(value: dict[str, object]) -> str:
        assert value is request
        await norma.evaluate_rules("native.step", agent_name="agent-alpha")
        return "ok"

    app.run = run

    def handler(outbound: httpx.Request) -> httpx.Response:
        if outbound.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if outbound.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if outbound.url.path.endswith("/checks"):
            observed.append(json.loads(outbound.content)["step"]["context"])
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma._checks.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "app.run",
                    "context": {"path": ["args", 0, "context"]},
                }
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        assert await app.run(request) == "ok"
        assert observed == [{"tenant": "tenant-a"}]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_v2_inline_native_options_capture_state_and_action_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    effects: list[str] = []
    checks: list[dict[str, object]] = []

    async def action(target: str) -> str:
        effects.append(target)
        return "ok"

    async def run() -> str:
        return await app.action(target="/repo/file.txt")

    app.action, app.run = action, run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma._instrumentation_operations.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    monkeypatch.setattr("norma.client.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {
                "write": {
                    "target": "app.action",
                    "options": {
                        "action_kind": "edit",
                        "action_target_arg": "target",
                        "state": {"approved": True},
                    },
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        assert await app.run() == "ok"
        assert effects == ["/repo/file.txt"]
        for request in checks:
            context = request["step"]["context"]
            assert context["state"] == {"approved": True}
            assert context["norma_action"]["kind"] == "edit"
            assert context["norma_action"]["target"] == "/repo/file.txt"
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_v2_inline_typescript_only_options_fail_before_agent_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(action=lambda: "ok", run=lambda: app.action())
    requests: list[str] = []

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(404)

        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    monkeypatch.setattr("norma.client.NormaClient", client_factory)
    config = {
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {
            "invoke": {
                "target": "app.action",
                "options": {"client": {"binding": "app.client"}},
            }
        },
    }

    with pytest.raises(ValueError, match="unsupported in Python: client"):
        await norma.aconfigure(
            config,
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={"runtime_url": URL, "agent_key": "key-alpha"},
        )

    assert requests == []
    assert app.action() == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_v2_operation_denial_blocks_original_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    allowed: bool,
) -> None:
    app = SimpleNamespace()
    effects: list[str] = []
    app.commit = lambda value: effects.append(value) or "committed"
    app.run = lambda: app.commit("write")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.method == "POST" and request.url.path.endswith("/operations"):
            proposal = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "operation-1",
                    "operation_key": proposal["operation_key"],
                    "agent_name": proposal["agent_name"],
                    "created_at": "2026-10-30T00:00:00Z",
                    "request_fingerprint": "fingerprint",
                    "request": proposal,
                    "decision": {
                        "outcome": "allow" if allowed else "deny",
                        "reason": "allowed by policy" if allowed else "blocked by policy",
                        "check": {
                            "allowed": allowed,
                            "confidence": 1,
                            "execution_contract": "hosted-v1",
                        },
                    },
                },
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma._instrumentation_operations.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    monkeypatch.setattr("norma.client.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {},
            "operations": {
                "commit": {
                    "target": "app.commit",
                    "operation_key": {"value": "release-42"},
                    "input": {"value": {"resource": "db"}},
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha"},
    )
    try:
        if allowed:
            assert app.run() == "committed"
            assert effects == ["write"]
        else:
            with pytest.raises(norma.CheckError, match="blocked by policy"):
                app.run()
            assert effects == []
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_v2_operation_uses_approved_argument_snapshot_after_admission_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    entered_operation = asyncio.Event()
    release_operation = asyncio.Event()
    effects: list[dict[str, object]] = []

    async def commit(payload: dict[str, object]) -> str:
        effects.append(payload)
        return "committed"

    async def run(payload: dict[str, object]) -> str:
        return await app.commit(payload)

    app.commit, app.run = commit, run

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.method == "POST" and request.url.path.endswith("/operations"):
            proposal = json.loads(request.content)
            entered_operation.set()
            await release_operation.wait()
            return httpx.Response(
                200,
                json={
                    "id": "operation-1",
                    "operation_key": proposal["operation_key"],
                    "agent_name": proposal["agent_name"],
                    "created_at": "2026-10-30T00:00:00Z",
                    "request_fingerprint": "fingerprint",
                    "request": proposal,
                    "decision": {
                        "outcome": "allow",
                        "reason": "allowed",
                        "check": {
                            "allowed": True,
                            "confidence": 1,
                            "execution_contract": "hosted-v1",
                        },
                    },
                },
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    for module in (
        "norma.instrumentation",
        "norma._instrumentation_operations",
        "norma.checkpoints",
        "norma.client",
    ):
        monkeypatch.setattr(f"{module}.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {},
            "operations": {
                "commit": {
                    "target": "app.commit",
                    "operation_key": {"value": "release-42"},
                    "input": {"path": ["args", 0]},
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha"},
    )
    payload = {"resource": "original"}
    try:
        task = asyncio.create_task(app.run(payload))
        await entered_operation.wait()
        payload["resource"] = "mutated while awaiting"
        release_operation.set()
        assert await task == "committed"
        assert effects == [{"resource": "original"}]
    finally:
        release_operation.set()
        await configured.aclose()


@pytest.mark.asyncio
async def test_v2_operation_reports_attempted_and_succeeded_without_claiming_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    action_results: list[str] = []
    observed: list[dict[str, object]] = []
    proposal: dict[str, object] | None = None

    def commit(value: dict[str, object], *, key: str = "release-42") -> str:
        action_results.append(str(value["resource"]))
        return "committed"

    app.commit = commit
    app.run = lambda: app.commit({"resource": "db"}, key="release-42")

    def record_body(observations: list[dict[str, object]]) -> dict[str, object]:
        assert proposal is not None
        return {
            "id": "operation-1",
            "operation_key": "release-42",
            "agent_name": "agent-alpha",
            "created_at": "2026-10-30T00:00:00Z",
            "request_fingerprint": "fingerprint",
            "request": proposal,
            "decision": {
                "outcome": "allow",
                "reason": "allowed",
                "check": {
                    "allowed": True,
                    "confidence": 1,
                    "execution_contract": "hosted-v1",
                },
            },
            "observations": observations,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal proposal
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.method == "POST" and request.url.path.endswith("/operations"):
            proposal = json.loads(request.content)
            return httpx.Response(200, json=record_body([]))
        if request.method == "POST" and request.url.path.endswith(
            "/operations/operation-1/observations"
        ):
            body = json.loads(request.content)
            observed.append(body)
            observed_record = {
                "id": f"observation-{len(observed)}",
                "event_key": body["event_key"],
                "kind": body["kind"],
                "summary": body["summary"],
                "external_reference": body.get("external_reference"),
                "recorded_at": "2026-10-30T00:00:01Z",
                "source": "caller_reported",
            }
            return httpx.Response(200, json=record_body([observed_record]))
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    for module in (
        "norma.instrumentation",
        "norma._instrumentation_operations",
        "norma.checkpoints",
        "norma.client",
    ):
        monkeypatch.setattr(f"{module}.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {},
            "operations": {
                "commit": {
                    "target": "app.commit",
                    "operation_key": {"path": ["params", "key"]},
                    "input": {"path": ["input"]},
                    "observations": {
                        "attempted": {
                            "event_key": {"value": "commit-attempted"},
                            "summary": {"value": "Commit was called"},
                        },
                        "succeeded": {
                            "event_key": {"value": "commit-succeeded"},
                            "summary": {"path": ["result"]},
                        },
                    },
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha"},
    )
    try:
        assert app.run() == "committed"
        assert action_results == ["db"]
        assert proposal is not None
        assert proposal["operation_key"] == "release-42"
        assert proposal["step"]["input"] == {"resource": "db"}
        assert [item["kind"] for item in observed] == ["attempted", "succeeded"]
        assert observed[1]["summary"] == "committed"
        assert all(item.get("source", "caller_reported") == "caller_reported" for item in observed)
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_v2_operation_is_bound_to_its_configured_agent_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    effects: list[str] = []
    app.action_a = lambda: effects.append("executed")
    app.entry_a = lambda: "a"
    app.entry_b = lambda: app.action_a()
    configs = tmp_path / "agents"
    configs.mkdir()
    (configs / "agent-alpha.json").write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": ["app.entry_a"],
                "checkpoints": {},
                "operations": {
                    "commit": {
                        "target": "app.action_a",
                        "operation_key": {"value": "release-42"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (configs / "agent-beta.json").write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": ["app.entry_b"],
                "checkpoints": {},
            }
        ),
        encoding="utf-8",
    )
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method + " " + request.url.path)
        if "/agents/agent-" in request.url.path and not request.url.path.endswith("/rules"):
            agent_name = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"agent": {"agent_name": agent_name}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    for module in (
        "norma.instrumentation",
        "norma._instrumentation_operations",
        "norma.checkpoints",
        "norma.client",
    ):
        monkeypatch.setattr(f"{module}.NormaClient", client_factory)
    configured = await norma.aconfigure(
        configs,
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-shared"},
    )
    try:
        with pytest.raises(norma.CheckError, match="configured entrypoint scope"):
            app.entry_b()
        assert effects == []
        assert not any(path.endswith("/operations") for path in requests)
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_v2_operation_close_during_admission_blocks_later_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    effects: list[str] = []
    entered_operation = asyncio.Event()
    release_operation = asyncio.Event()

    async def action() -> str:
        effects.append("executed")
        return "ok"

    async def run() -> str:
        return await app.action()

    app.action, app.run = action, run

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.method == "POST" and request.url.path.endswith("/operations"):
            proposal = json.loads(request.content)
            entered_operation.set()
            await release_operation.wait()
            return httpx.Response(
                200,
                json={
                    "id": "operation-1",
                    "operation_key": proposal["operation_key"],
                    "agent_name": proposal["agent_name"],
                    "created_at": "2026-10-30T00:00:00Z",
                    "request_fingerprint": "fingerprint",
                    "request": proposal,
                    "decision": {
                        "outcome": "allow",
                        "reason": "allowed",
                        "check": {
                            "allowed": True,
                            "confidence": 1,
                            "execution_contract": "hosted-v1",
                        },
                    },
                },
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    for module in (
        "norma.instrumentation",
        "norma._instrumentation_operations",
        "norma.checkpoints",
        "norma.client",
    ):
        monkeypatch.setattr(f"{module}.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["app.run"],
            "checkpoints": {},
            "operations": {
                "commit": {
                    "target": "app.action",
                    "operation_key": {"value": "release-42"},
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha"},
    )
    task = asyncio.create_task(app.run())
    try:
        await entered_operation.wait()
        await configured.aclose()
        release_operation.set()
        with pytest.raises(norma.CheckError, match="closed or changed"):
            await task
        assert effects == []
    finally:
        release_operation.set()
        await configured.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("deny_parent", [False, True])
async def test_v2_parent_checkpoint_composes_around_child_entrypoint_and_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deny_parent: bool,
) -> None:
    app = SimpleNamespace()
    calls: list[str] = []

    async def child_entry() -> str:
        calls.append("child-body")
        return "child-result"

    async def parent_entry() -> str:
        calls.append("parent-before")
        result = await app.child_entry()
        calls.append("parent-after")
        return result

    app.child_entry, app.parent_entry = child_entry, parent_entry
    configs = tmp_path / "agents"
    configs.mkdir()
    (configs / "agent-alpha.json").write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": ["app.parent_entry"],
                "checkpoints": {
                    "parent_bridge": {"target": "app.child_entry"},
                },
            }
        ),
        encoding="utf-8",
    )
    (configs / "agent-beta.json").write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": ["app.child_entry"],
                "checkpoints": {
                    "child_body": {"target": "app.child_entry"},
                },
            }
        ),
        encoding="utf-8",
    )
    checked: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/agents/agent-" in request.url.path and not request.url.path.endswith("/rules"):
            agent_name = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"agent": {"agent_name": agent_name}})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            payload = json.loads(request.content)
            checked.append((payload["step"]["name"], request.headers["X-API-Key"]))
            if deny_parent and payload["step"]["name"] == "parent_bridge":
                return httpx.Response(
                    200,
                    json={**SAFE, "allowed": False, "reason": "parent denied"},
                )
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    for module in (
        "norma.instrumentation",
        "norma.checkpoints",
        "norma.client",
    ):
        monkeypatch.setattr(f"{module}.NormaClient", client_factory)
    configured = await norma.aconfigure(
        configs,
        bindings={"app": app},
        connections={
            "agent-alpha": {"runtime_url": URL, "agent_key": "key-alpha"},
            "agent-beta": {"runtime_url": URL, "agent_key": "key-beta"},
        },
    )
    try:
        if deny_parent:
            with pytest.raises(norma.RuleViolationError):
                await app.parent_entry()
            assert calls == ["parent-before"]
            assert [name for name, _ in checked] == ["parent_bridge"]
        else:
            assert await app.parent_entry() == "child-result"
            assert calls == ["parent-before", "child-body", "parent-after"]
            assert [name for name, _ in checked] == [
                "parent_bridge",
                "child_body",
                "child_body",
                "parent_bridge",
            ]
            assert [key for _, key in checked] == [
                "key-alpha",
                "key-beta",
                "key-beta",
                "key-alpha",
            ]
    finally:
        await configured.aclose()


def _write_config(directory: Path, name: str, entrypoint: str) -> None:
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entrypoints": [entrypoint],
                "checkpoints": {"shared_action": {"target": "app.action", "step_type": "tool"}},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_shared_checkpoint_dispatch_isolates_agent_auth_and_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_schemas = norma.checkpoint_schemas()
    effects: list[str] = []
    app = SimpleNamespace()

    async def action(value: str) -> str:
        effects.append(value)
        return value

    async def run_alpha(value: str) -> str:
        return await app.action(value)

    async def run_bravo(value: str) -> str:
        return await app.action(value)

    app.action = action
    app.run_alpha = run_alpha
    app.run_bravo = run_bravo
    original_action = app.action
    original_alpha = app.run_alpha
    original_bravo = app.run_bravo
    _write_config(tmp_path, "agent-alpha", "app.run_alpha")
    _write_config(tmp_path, "agent-bravo", "app.run_bravo")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if path.endswith("/agents/agent-bravo"):
            return httpx.Response(200, json={"agent_name": "agent-bravo"})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            body = json.loads(request.content)
            if body["agent_name"] == "agent-alpha":
                return httpx.Response(
                    200,
                    json={
                        **SAFE,
                        "unresolved": [
                            {
                                "rule_id": 7,
                                "rule_name": "deny-alpha",
                                "action": "deny",
                                "result": {"matched": False, "confidence": 0},
                            }
                        ],
                    },
                )
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404, json={"detail": "unexpected path"})

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    monkeypatch.setattr("norma.client.NormaClient", client_factory)
    configured = await norma.aconfigure(
        tmp_path,
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
        connections={
            "agent-alpha": {"agent_key": "key-alpha", "target_type": "team", "target_id": "a"},
            "agent-bravo": {"agent_key": "key-bravo", "target_type": "team", "target_id": "b"},
        },
    )
    assert set(configured.schemas) == {"agent-alpha", "agent-bravo"}
    assert norma.checkpoint_schemas() == baseline_schemas
    assert norma.current_agent() is None
    with configured.scope("agent-alpha"):
        assert norma.current_agent().agent_name == "agent-alpha"
        with configured.scope("agent-bravo"):
            assert norma.current_agent().agent_name == "agent-bravo"
        assert norma.current_agent().agent_name == "agent-alpha"
    assert norma.current_agent() is None
    async with configured.client("agent-alpha") as scoped_client:
        assert (await norma.agents.get_agent(scoped_client, "agent-alpha"))[
            "agent_name"
        ] == "agent-alpha"

    with pytest.raises(norma.CheckError, match="entrypoint scope"):
        await app.action("unscoped")
    results = await asyncio.gather(
        app.run_alpha("blocked"), app.run_bravo("allowed"), return_exceptions=True
    )
    assert isinstance(results[0], norma.RuleViolationError)
    assert results[1] == "allowed"
    assert effects == ["allowed"]

    checks = [request for request in requests if request.url.path.endswith("/checks")]
    assert {json.loads(request.content)["agent_name"] for request in checks} == {
        "agent-alpha",
        "agent-bravo",
    }
    assert {request.headers["X-API-Key"] for request in checks} == {"key-alpha", "key-bravo"}
    explicit_client_gets = [
        request
        for request in requests
        if request.method == "GET" and request.url.path.endswith("/agents/agent-alpha")
    ]
    assert explicit_client_gets[-1].headers["X-API-Key"] == "key-alpha"
    assert {
        (
            json.loads(request.content).get("target_type"),
            json.loads(request.content).get("target_id"),
        )
        for request in checks
    } == {("team", "a"), ("team", "b")}
    assert not [
        request
        for request in requests
        if request.method == "POST" and request.url.path.endswith("/agents/register")
    ]

    receipts = configured.close()
    assert set(receipts) == {"agent-alpha", "agent-bravo"}
    assert configured.close() == receipts
    assert app.action is original_action
    assert app.run_alpha is original_alpha
    assert app.run_bravo is original_bravo


@pytest.mark.asyncio
async def test_all_local_files_validate_before_any_remote_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SimpleNamespace()

    def original() -> str:
        return "ok"

    app.run = original
    (tmp_path / "agent-alpha.json").write_text(
        '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}', encoding="utf-8"
    )
    (tmp_path / "agent-bravo.json").write_text(
        '{"version":1,"entrypoints":["app.missing"],"checkpoints":{}}', encoding="utf-8"
    )
    calls = 0

    def no_network(**_: object) -> NormaClient:
        nonlocal calls
        calls += 1
        raise AssertionError("network started before local validation completed")

    monkeypatch.setattr("norma.instrumentation.NormaClient", no_network)
    with pytest.raises(ValueError, match="does not exist"):
        await norma.aconfigure(tmp_path, bindings={"app": app})
    assert calls == 0
    assert app.run is original


@pytest.mark.asyncio
async def test_in_memory_config_is_copied_before_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()

    async def action() -> str:
        return "ok"

    async def run() -> str:
        return await app.action()

    app.action, app.run = action, run
    config = {
        "version": 1,
        "entrypoints": ["app.run"],
        "checkpoints": {"act": {"target": "app.action", "state": {"mode": "safe"}}},
    }
    before = json.loads(json.dumps(config))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    assert config == before
    config["checkpoints"]["act"]["state"]["mode"] = "mutated"
    assert await app.run() == "ok"
    assert config["checkpoints"]["act"]["state"]["mode"] == "mutated"
    configured.close()


@pytest.mark.asyncio
async def test_missing_later_agent_key_fails_before_any_remote_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SimpleNamespace(run_a=lambda: None, run_b=lambda: None)
    _write_empty_config(tmp_path, "agent-alpha", "app.run_a")
    _write_empty_config(tmp_path, "agent-bravo", "app.run_b")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(200, json={"agent_name": "agent-alpha"})

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    with pytest.raises(ValueError, match="AGENT_KEY is required"):
        await norma.aconfigure(
            tmp_path,
            bindings={"app": app},
            connection={"runtime_url": URL, "observability_enabled": False},
            connections={
                "agent-alpha": {"agent_key": "key-alpha"},
                "agent-bravo": {"agent_key": ""},
            },
        )
    assert requests == []
    assert app.run_a.__name__ == "<lambda>"
    assert app.run_b.__name__ == "<lambda>"


def test_in_memory_config_requires_exact_agent_name() -> None:
    config = {"version": 1, "entrypoints": ["app.run"], "checkpoints": {}}
    with pytest.raises(ValueError, match="required"):
        norma.configure(config, bindings={"app": object()})
    with pytest.raises(ValueError, match="normalized"):
        norma.configure(config, agent_name=" Agent-Alpha ", bindings={"app": object()})


def test_dict_subclass_binding_is_rejected_before_attribute_or_item_mutation() -> None:
    class App(dict[str, object]):
        pass

    app = App(run=lambda: None)
    original = app["run"]
    with pytest.raises(ValueError, match="dict subclass"):
        norma.configure(
            {"version": 1, "entrypoints": ["app.run"], "checkpoints": {}},
            agent_name="agent-alpha",
            bindings={"app": app},
        )
    assert app["run"] is original


def test_callable_instance_target_is_rejected_before_network_or_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class AsyncCallable:
        async def __call__(self) -> str:
            calls.append("called")
            return "unsafe"

    app = SimpleNamespace(target=AsyncCallable())

    def no_network(**_: object) -> NormaClient:
        raise AssertionError("callable target should be rejected before network")

    monkeypatch.setattr("norma.instrumentation.NormaClient", no_network)
    with pytest.raises(ValueError, match="ordinary function or bound method"):
        norma.configure(
            {
                "version": 1,
                "entrypoints": ["app.target"],
                "checkpoints": {},
            },
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={
                "runtime_url": URL,
                "agent_key": "key-alpha",
                "observability_enabled": False,
                "refresh_interval_seconds": 0,
            },
        )
    assert calls == []


@pytest.mark.asyncio
async def test_dispatcher_checks_session_identity_when_handles_share_agent_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    effects: list[str] = []

    async def action_a(value: str) -> str:
        effects.append(f"a:{value}")
        return value

    async def action_b(value: str) -> str:
        effects.append(f"b:{value}")
        return value

    async def run_a(value: str) -> str:
        return await app.action_b(value)

    async def run_b(value: str) -> str:
        return await app.action_b(value)

    app.action_a = action_a
    app.action_b = action_b
    app.run_a = run_a
    app.run_b = run_b

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(200, json=SAFE)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    shared = {"runtime_url": URL, "observability_enabled": False, "refresh_interval_seconds": 0}
    first = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run_a"],
            "checkpoints": {"action_a": {"target": "app.action_a"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection=shared,
    )
    second = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run_b"],
            "checkpoints": {"action_b": {"target": "app.action_b"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection=shared,
    )
    with pytest.raises(norma.CheckError, match="not configured"):
        await app.run_a("cross-handle")
    assert await app.run_b("local") == "local"
    assert effects == ["b:local"]
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_nested_entrypoints_restore_the_outer_agent_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SimpleNamespace()
    checks: list[tuple[str, str]] = []

    async def action(label: str) -> str:
        return label

    async def inner() -> str:
        return await app.action("inner")

    async def outer() -> str:
        await app.action("before")
        result = await app.inner()
        await app.action("after")
        return result

    app.action, app.inner, app.outer = action, inner, outer
    _write_config(tmp_path, "agent-alpha", "app.outer")
    _write_config(tmp_path, "agent-bravo", "app.inner")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if path.endswith("/agents/agent-bravo"):
            return httpx.Response(200, json={"agent_name": "agent-bravo"})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            body = json.loads(request.content)
            checks.append((body["agent_name"], request.headers["X-API-Key"]))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        tmp_path,
        bindings={"app": app},
        connection={"runtime_url": URL, "refresh_interval_seconds": 0},
        connections={
            "agent-alpha": {"agent_key": "key-alpha"},
            "agent-bravo": {"agent_key": "key-bravo"},
        },
    )
    assert await app.outer() == "inner"
    assert checks == [
        ("agent-alpha", "key-alpha"),
        ("agent-alpha", "key-alpha"),
        ("agent-bravo", "key-bravo"),
        ("agent-bravo", "key-bravo"),
        ("agent-alpha", "key-alpha"),
        ("agent-alpha", "key-alpha"),
    ]
    configured.close()


@pytest.mark.asyncio
async def test_mutated_scoped_agent_identity_blocks_before_action_or_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    effects: list[str] = []

    async def action() -> str:
        effects.append("ran")
        return "ok"

    async def run() -> str:
        return await app.action()

    app.action, app.run = action, run
    check_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal check_requests
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            check_requests += 1
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run"],
            "checkpoints": {"action": {"target": "app.action"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    with configured.scope("agent-alpha"):
        selected = norma.current_agent()
        assert selected is not None
        selected.agent_name = "agent-bravo"
    with pytest.raises(norma.CheckError, match="identity changed"):
        await app.run()
    assert effects == []
    assert check_requests == 0
    configured.close()


@pytest.mark.asyncio
async def test_child_task_keeps_scope_after_entrypoint_and_fails_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    checks: list[str] = []

    async def action(value: str) -> str:
        return value

    async def delayed(target: object, gate: asyncio.Event) -> str:
        await gate.wait()
        return await target("child")  # type: ignore[operator]

    async def run(gate: asyncio.Event) -> asyncio.Task[str]:
        target = app.action
        return asyncio.create_task(delayed(target, gate))

    app.action, app.run = action, run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            checks.append(request.headers["X-API-Key"])
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run"],
            "checkpoints": {"action": {"target": "app.action"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    first_gate = asyncio.Event()
    first_task = await app.run(first_gate)
    first_gate.set()
    assert await first_task == "child"
    assert checks == ["key-alpha", "key-alpha"]

    second_gate = asyncio.Event()
    stale_task = await app.run(second_gate)
    configured.close()
    second_gate.set()
    with pytest.raises(norma.CheckError, match="scope is required"):
        await stale_task


def test_sync_configure_preserves_instance_class_and_static_method_receivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runner:
        def instance_entry(self) -> str:
            return f"instance:{self.label}"

        @classmethod
        def class_entry(cls) -> str:
            return f"class:{cls.__name__}"

        @staticmethod
        def static_entry() -> str:
            return "static"

    runner = Runner()
    runner.label = "kept"
    original_instance = Runner.instance_entry
    original_class = vars(Runner)["class_entry"]
    original_static = vars(Runner)["static_entry"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    configured = norma.configure(
        {
            "version": 1,
            "entrypoints": [
                "app.instance_entry",
                "Runner.class_entry",
                "Runner.static_entry",
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": runner, "Runner": Runner},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    assert runner.instance_entry() == "instance:kept"
    assert Runner.class_entry() == "class:Runner"
    assert Runner.static_entry() == "static"
    configured.close()
    assert runner.instance_entry.__func__ is original_instance
    assert vars(Runner)["class_entry"] is original_class
    assert vars(Runner)["static_entry"] is original_static


@pytest.mark.asyncio
async def test_named_state_provider_and_action_options_are_captured_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SimpleNamespace()
    provider_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    requests: list[dict[str, object]] = []

    async def action(target: str, command: str, cwd: str) -> str:
        return "done"

    async def run() -> str:
        return await app.action("/tmp/file", "write", "/tmp")

    app.action, app.run = action, run
    config = {
        "version": 1,
        "entrypoints": ["app.run"],
        "checkpoints": {
            "edit": {
                "target": "app.action",
                "step_type": "tool",
                "state_provider": "snapshot",
                "options": "edit_action",
            }
        },
    }

    def snapshot(*args: object, **kwargs: object) -> dict[str, object]:
        provider_calls.append((args, kwargs))
        return {"version": len(provider_calls), "mode": "safe"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app},
        providers={"snapshot": snapshot},
        options={
            "edit_action": {
                "action_kind": "edit",
                "action_target_arg": "target",
                "action_command_arg": "command",
                "action_cwd_arg": "cwd",
            }
        },
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    assert await app.run() == "done"
    assert len(provider_calls) == 1
    assert provider_calls[0][0] == ("/tmp/file", "write", "/tmp")
    assert [item["stage"] for item in requests] == ["pre", "post"]
    for payload in requests:
        step = payload["step"]
        assert step["type"] == "tool"
        assert step["context"]["state"] == {"version": 1, "mode": "safe"}
        assert step["context"]["norma_action"] == {
            "kind": "edit",
            "target": "/tmp/file",
            "command": "write",
            "cwd": "/tmp",
        }
    configured.close()


@pytest.mark.asyncio
async def test_v2_value_sources_resolve_state_and_action_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effects: list[str] = []
    provider_calls: list[dict[str, object]] = []
    checks: list[dict[str, object]] = []
    scoped_checks: list[tuple[dict[str, object], str | None, str | None]] = []

    class Application(SimpleNamespace):
        def capture_state(self, invocation: dict[str, object]) -> dict[str, object]:
            provider_calls.append(invocation)
            kwargs = invocation["kwargs"]
            assert isinstance(kwargs, dict)
            return {"approved_target": kwargs["target"]}

    app = Application()
    app.logging_config = {"version": 1, "disable_existing_loggers": False}

    async def action(target: str, command: str, cwd: str) -> str:
        effects.append(target)
        return "written"

    async def run() -> str:
        return await app.action(target="/repo/a.txt", command="write", cwd="/repo")

    app.action, app.run = action, run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            check = json.loads(request.content)
            checks.append(check)
            scoped_checks.append(
                (check, request.headers.get("X-Trace-Id"), request.headers.get("X-Span-Id"))
            )
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    app.transport = transport
    client_options: list[dict[str, object]] = []

    def client_factory(
        *,
        runtime_url: str,
        agent_key: str,
        control_contract: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> NormaClient:
        client_options.append(
            {"timeout": timeout, "transport": transport, "runtime_url": runtime_url}
        )
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            timeout=timeout,
            transport=transport,
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    monkeypatch.setattr("norma.client.NormaClient", client_factory)
    logging_configs: list[dict[str, object]] = []

    def capture_logging_config(config: dict[str, object]) -> object:
        logging_configs.append(config)
        return object()

    monkeypatch.setattr("norma.observability.configure_logging", capture_logging_config)
    configured = await norma.aconfigure(
        {
            "version": 2,
            "connection": {
                "timeout_ms": 2500,
                "transport": {"binding": "app.transport"},
            },
            "telemetry": {
                "enabled": False,
                "max_retries": 2,
                "retry_delay": 0.5,
                "shutdown_join_timeout": 0.6,
                "shutdown_flush_timeout": 0.7,
                "shutdown_max_failed_flushes": 2,
                "logging": {
                    "config": {"binding": "app.logging_config"},
                    "log_rule_eval": False,
                    "log_span_results": False,
                },
            },
            "entrypoints": [
                {
                    "target": "app.run",
                    "context": {"value": {"tenant": "team-9"}},
                    "target_scope": {
                        "type": {"value": "team"},
                        "id": {"path": ["context", "tenant"]},
                    },
                    "trace": {
                        "trace_id": {"value": "0123456789abcdef0123456789abcdef"},
                        "span_id": {"value": "0123456789abcdef"},
                    },
                }
            ],
            "checkpoints": {
                "edit": {
                    "target": "app.action",
                    "state_from": {"provider": "app.capture_state"},
                    "input_from": {"value": {"target": "/repo/a.txt"}},
                    "output_from": {"path": ["result"]},
                    "context_from": {"value": {"approved": "context"}},
                    "tools_from": {"value": [{"name": "write_file"}]},
                    "ground_truth_from": {"value": {"status": "written"}},
                    "action": {
                        "kind": "edit",
                        "target": {"path": ["kwargs", "target"]},
                        "command": {"path": ["kwargs", "command"]},
                        "cwd": {"path": ["kwargs", "cwd"]},
                    },
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "timeout_ms": 3000,
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert configured._sessions["agent-alpha"].client_transport is transport
        assert await app.run() == "written"
        assert effects == ["/repo/a.txt"]
        assert len(client_options) >= 2
        assert all(option["timeout"] == 3.0 for option in client_options)
        assert all(option["transport"] is transport for option in client_options)
        settings = configured._sessions["agent-alpha"].observability_settings
        assert settings is not None
        assert settings.max_retries == 2
        assert settings.retry_delay == 0.5
        assert settings.shutdown_join_timeout == 0.6
        assert settings.shutdown_flush_timeout == 0.7
        assert settings.shutdown_max_failed_flushes == 2
        assert settings.log_rule_eval is False
        assert settings.log_span_results is False
        assert logging_configs == [app.logging_config]
        assert len(provider_calls) == 1
        assert set(provider_calls[0]) == {
            "args",
            "kwargs",
            "result",
            "error",
            "context",
            "agent_name",
        }
        assert provider_calls[0]["agent_name"] == "agent-alpha"
        assert provider_calls[0]["context"] == {"tenant": "team-9"}
        assert [request["stage"] for request in checks] == ["pre", "post"]
        assert all(item[0]["target_type"] == "team" for item in scoped_checks)
        assert all(item[0]["target_id"] == "team-9" for item in scoped_checks)
        assert all(item[1] == "0123456789abcdef0123456789abcdef" for item in scoped_checks)
        assert all(item[2] == "0123456789abcdef" for item in scoped_checks)
        pre_step, post_step = checks[0]["step"], checks[1]["step"]
        assert pre_step["input"] == {"target": "/repo/a.txt"}
        assert post_step["input"] == pre_step["input"]
        assert pre_step["output"] is None
        assert post_step["output"] == "written"
        assert pre_step["context"]["approved"] == "context"
        assert post_step["context"]["approved"] == "context"
        assert pre_step["tools"] == [{"name": "write_file"}]
        assert post_step["tools"] == pre_step["tools"]
        assert pre_step["ground_truth"] == {"status": "written"}
        assert post_step["ground_truth"] == pre_step["ground_truth"]
        for request in checks:
            step = request["step"]
            assert step["context"]["state"] == {"approved_target": "/repo/a.txt"}
            assert step["context"]["norma_action"] == {
                "kind": "edit",
                "target": "/repo/a.txt",
                "command": "write",
                "cwd": "/repo",
            }
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_close_during_precheck_blocks_original_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    effects: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    async def action(value: str) -> str:
        effects.append(value)
        return value

    async def run(value: str) -> str:
        return await app.action(value)

    app.action = action
    app.run = run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            entered.set()
            assert release.wait(3)
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.checkpoints.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run"],
            "checkpoints": {"action": {"target": "app.action"}},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )

    def run_in_thread() -> object:
        return asyncio.run(app.run("must-not-run"))

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(run_in_thread)
        assert await asyncio.to_thread(entered.wait, 2)
        configured.close()
        release.set()
        with pytest.raises(norma.CheckError, match="session changed"):
            await asyncio.to_thread(pending.result, 3)
    assert effects == []


@pytest.mark.asyncio
async def test_target_replaced_during_authorization_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()

    def original() -> str:
        return "original"

    def replacement() -> str:
        return "replacement"

    app.run = original
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method + " " + request.url.path)
        if request.url.path.endswith("/agents/agent-alpha"):
            app.run = replacement
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    with pytest.raises(ValueError, match="changed during configuration"):
        await norma.aconfigure(
            {
                "version": 1,
                "entrypoints": ["app.run"],
                "checkpoints": {},
            },
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={"runtime_url": URL, "observability_enabled": False},
        )
    assert app.run is replacement
    assert not any("/agents/register" in request for request in requests)


@pytest.mark.asyncio
async def test_rule_refresh_runs_when_observability_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    refreshed = threading.Event()
    rules_requests = 0

    def run() -> str:
        return norma.current_agent().agent_name

    app.run = run

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal rules_requests
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            rules_requests += 1
            if rules_requests > 1:
                refreshed.set()
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run"],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "observability_enabled": False,
            "refresh_interval_seconds": 1,
        },
    )
    assert app.run() == "agent-alpha"
    assert await asyncio.to_thread(refreshed.wait, 3)
    receipts = configured.uninstall()
    assert set(receipts) == {"agent-alpha"}


@pytest.mark.asyncio
async def test_finish_checks_use_each_entrypoint_agent_and_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from norma.finish import TurnRecorder, check_finish

    app = SimpleNamespace()

    async def finish_alpha() -> object:
        return await check_finish(
            TurnRecorder(session_id="s", turn_id="a", user_request="A").turn("Done")
        )

    async def finish_bravo() -> object:
        return await check_finish(
            TurnRecorder(session_id="s", turn_id="b", user_request="B").turn("Done")
        )

    app.finish_alpha = finish_alpha
    app.finish_bravo = finish_bravo
    _write_empty_config(tmp_path, "agent-alpha", "app.finish_alpha")
    _write_empty_config(tmp_path, "agent-bravo", "app.finish_bravo")
    checks: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/agents/agent-bravo"):
            return httpx.Response(200, json={"agent_name": "agent-bravo"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            body = json.loads(request.content)
            checks.append((body["agent_name"], request.headers["X-API-Key"]))
            return httpx.Response(
                200,
                json={
                    "execution_contract": "hosted-v1",
                    "verdict": "pass",
                    "reason": "Complete",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": {"count": 0, "max": 2},
                    "allow_finish": True,
                    "outcome": "completed",
                },
            )
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.finish.NormaClient", client_factory)
    configured = await norma.aconfigure(
        tmp_path,
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
        connections={
            "agent-alpha": {"agent_key": "key-alpha"},
            "agent-bravo": {"agent_key": "key-bravo"},
        },
    )
    verdicts = await asyncio.gather(app.finish_alpha(), app.finish_bravo())
    assert [verdict.verdict for verdict in verdicts] == ["pass", "pass"]
    assert set(checks) == {("agent-alpha", "key-alpha"), ("agent-bravo", "key-bravo")}
    previous_count = len(checks)
    with configured.scope("agent-alpha"):
        with pytest.raises(norma.CheckError, match="must match the selected"):
            await norma.evaluate_rules("native.step", agent_name="agent-bravo")
        mismatched = await check_finish(
            TurnRecorder(session_id="s", turn_id="wrong", user_request="wrong").turn("Done"),
            agent_name="agent-bravo",
        )
    assert mismatched.verdict == "unavailable"
    assert len(checks) == previous_count
    configured.close()


@pytest.mark.asyncio
async def test_scoped_event_batchers_keep_agent_credentials_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from norma.models import RuleCheckEvent
    from norma.observability import add_event

    app = SimpleNamespace()

    def event(agent_name: str) -> RuleCheckEvent:
        return RuleCheckEvent(
            trace_id=f"trace-{agent_name}",
            span_id=f"span-{agent_name}",
            agent_name=agent_name,
            rule_id=1,
            rule_name="scoped",
            check_stage="pre",
            applies_to="tool_call",
            action="observe",
            matched=False,
            confidence=1.0,
        )

    def emit_alpha() -> None:
        add_event(event("agent-alpha"))

    def emit_bravo() -> None:
        add_event(event("agent-bravo"))

    app.emit_alpha, app.emit_bravo = emit_alpha, emit_bravo
    _write_empty_config(tmp_path, "agent-alpha", "app.emit_alpha")
    _write_empty_config(tmp_path, "agent-bravo", "app.emit_bravo")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/agents/agent-bravo"):
            return httpx.Response(200, json={"agent_name": "agent-bravo"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    configured = await norma.aconfigure(
        tmp_path,
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "observability_enabled": True,
            "batch_size": 10,
            "flush_interval": 60.0,
            "refresh_interval_seconds": 0,
        },
        connections={
            "agent-alpha": {"agent_key": "key-alpha"},
            "agent-bravo": {"agent_key": "key-bravo"},
        },
    )
    delivered: list[tuple[str, str]] = []

    class FakeAsyncClient:
        def __init__(self, **_: object) -> None:
            pass

        async def post(
            self, url: str, *, json: dict[str, object], headers: dict[str, str]
        ) -> httpx.Response:
            for item in json["events"]:
                delivered.append((headers["X-API-Key"], item["agent_name"]))
            count = len(json["events"])
            return httpx.Response(
                202,
                json={
                    "received": count,
                    "enqueued": count,
                    "dropped": 0,
                    "duplicates": 0,
                    "stored": count,
                    "status": "queued",
                },
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr("norma.observability.httpx.AsyncClient", FakeAsyncClient)
    app.emit_alpha()
    app.emit_bravo()
    receipts = await asyncio.gather(
        configured.aflush("agent-alpha"), configured.aflush("agent-bravo")
    )
    assert all(receipt.acknowledged == 1 for receipt in receipts)
    assert set(delivered) == {("key-alpha", "agent-alpha"), ("key-bravo", "agent-bravo")}
    await configured.aclose()


@pytest.mark.asyncio
async def test_registered_event_sink_remains_application_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma._telemetry.sinks import SinkResult
    from norma.models import RuleCheckEvent
    from norma.observability import (
        add_event,
        register_rule_event_sink,
        unregister_rule_event_sink,
    )

    app = SimpleNamespace()
    delivered: list[str] = []

    class Sink:
        def write_events(self, events: list[RuleCheckEvent]) -> SinkResult:
            delivered.extend(item.agent_name for item in events)
            return SinkResult(accepted=len(events))

    sink = Sink()
    register_rule_event_sink(sink)

    def run() -> None:
        add_event(
            RuleCheckEvent(
                trace_id="trace",
                span_id="span",
                agent_name="agent-alpha",
                rule_id=1,
                rule_name="owned",
                check_stage="pre",
                applies_to="tool_call",
                action="observe",
                matched=False,
                confidence=1.0,
            )
        )

    app.run = run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {"version": 1, "entrypoints": ["app.run"], "checkpoints": {}},
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": True,
            "observability_sink_name": "registered",
            "refresh_interval_seconds": 0,
        },
    )
    app.run()
    receipt = configured.close()["agent-alpha"]
    assert delivered == ["agent-alpha"]
    assert receipt.status == "unconfirmed"
    unregister_rule_event_sink(sink)


def _write_empty_config(directory: Path, name: str, entrypoint: str) -> None:
    (directory / f"{name}.json").write_text(
        json.dumps({"version": 1, "entrypoints": [entrypoint], "checkpoints": {}}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_google_adk_plugin_binds_to_selected_configured_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("google.adk.agents.callback_context")
    from google.adk.agents import Agent

    from norma.integrations.google_adk import NormaPlugin

    app = SimpleNamespace()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/register"):
            return httpx.Response(200, json={"created": False, "rules": []})
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(
                200,
                json={"agent": {"agent_name": "agent-alpha"}, "steps": []},
            )
        if request.url.path.endswith("/agents/agent-bravo"):
            return httpx.Response(
                200,
                json={"agent": {"agent_name": "agent-bravo"}, "steps": []},
            )
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma.integrations.google_adk.plugin.NormaClient", client_factory)

    async def run() -> None:
        def action() -> str:
            """Synthetic action used to exercise ADK schema registration."""
            return "ok"

        plugin = NormaPlugin("agent-alpha", enabled_hooks=set())
        try:
            plugin.bind(Agent(name="agent_alpha", model="gemini-2.0-flash", tools=[action]))
        finally:
            await plugin.close()

    app.run = run
    configured = await norma.aconfigure(
        {
            "version": 1,
            "entrypoints": ["app.run"],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )

    await app.run()
    registration_requests = [
        request for request in requests if request.url.path.endswith("/register")
    ]
    assert registration_requests
    assert all(request.headers["X-API-Key"] == "key-alpha" for request in registration_requests)
    with pytest.raises(ValueError, match="match the currently initialized"):
        with configured.scope("agent-alpha"):
            NormaPlugin("agent-bravo", enabled_hooks=set())
    configured.close()


@pytest.mark.asyncio
async def test_strands_adapter_check_uses_configured_session_identity_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("strands")
    from norma.integrations.strands import NormaPlugin

    app = SimpleNamespace()
    checks: list[tuple[str, str]] = []

    async def run() -> None:
        plugin = NormaPlugin("agent-alpha", enable_logging=False)
        await plugin._evaluate_and_enforce("native.tool", input={"x": 1}, step_type="tool")

    app.run = run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            body = json.loads(request.content)
            checks.append((body["agent_name"], request.headers["X-API-Key"]))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma._checks.NormaClient", client_factory)
    configured = await norma.aconfigure(
        {"version": 1, "entrypoints": ["app.run"], "checkpoints": {}},
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    await app.run()
    assert checks == [("agent-alpha", "key-alpha")]
    configured.close()


@pytest.mark.asyncio
async def test_configured_strands_adapter_installs_and_restores_only_owned_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("strands")
    from strands import Agent
    from strands.hooks.events import BeforeInvocationEvent

    from norma._state import state as legacy_state

    app = SimpleNamespace()
    agent = Agent()
    initial_callbacks = {
        event_type: list(entries)
        for event_type, entries in vars(agent.hooks)["_registered_callbacks"].items()
    }
    legacy_agent = norma.Agent(agent_name="legacy-agent")
    monkeypatch.setattr(legacy_state, "current_agent", legacy_agent)
    checks: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            body = json.loads(request.content)
            checks.append((body["agent_name"], request.headers["X-API-Key"]))
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma._checks.NormaClient", client_factory)

    async def run() -> None:
        entries = vars(agent.hooks)["_registered_callbacks"][BeforeInvocationEvent]
        callback = next(
            entry.callback
            for entry in entries
            if getattr(entry.callback, "__self__", None).__class__.__name__ == "NormaPlugin"
        )
        await callback(
            BeforeInvocationEvent(
                agent=agent,
                messages=[{"role": "user", "content": "hello"}],
            )
        )

    app.run = run
    config = {
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {},
        "adapters": {
            "native": {
                "kind": "strands",
                "agent": {"binding": "native.agent"},
                "enable_logging": False,
            }
        },
    }
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app, "native": SimpleNamespace(agent=agent)},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert (
            len(vars(agent.hooks)["_registered_callbacks"].get(BeforeInvocationEvent, []))
            == len(initial_callbacks.get(BeforeInvocationEvent, [])) + 1
        )
        await app.run()
        assert checks == [("agent-alpha", "key-alpha")]
    finally:
        configured.close()
    assert vars(agent.hooks)["_registered_callbacks"] == initial_callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_invocation", [False, True])
@pytest.mark.parametrize("invalidate_session_during_check", [False, True])
async def test_configured_strands_adapter_runs_stubbed_agent_with_context_and_target(
    monkeypatch: pytest.MonkeyPatch,
    sync_invocation: bool,
    invalidate_session_during_check: bool,
) -> None:
    pytest.importorskip("strands")
    from collections.abc import AsyncGenerator

    from strands import Agent
    from strands.models.model import Model

    from norma._state import _StateContainer

    model_calls = 0

    class StubModel(Model):
        def update_config(self, **_kwargs: object) -> None:
            return None

        def get_config(self) -> dict[str, object]:
            return {}

        async def structured_output(
            self, *_args: object, **_kwargs: object
        ) -> AsyncGenerator[dict[str, object], None]:
            if False:
                yield {}

        async def stream(
            self, *_args: object, **_kwargs: object
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal model_calls
            model_calls += 1
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {"text": ""}, "contentBlockIndex": 0}}
            yield {
                "contentBlockDelta": {
                    "delta": {"text": "stubbed"},
                    "contentBlockIndex": 0,
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}}

    app = SimpleNamespace()
    agent = Agent(model=StubModel())
    checks: list[tuple[dict[str, object], str, str]] = []
    selected_session: list[_StateContainer] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            body = json.loads(request.content)
            if invalidate_session_during_check and selected_session:
                selected_session[0].session_generation = selected_session[0].session_generation + 1
            checks.append(
                (
                    body.get("step", {}).get("context", {}),
                    body.get("target_type", ""),
                    body.get("target_id", ""),
                )
            )
            return httpx.Response(200, json=SAFE)
        return httpx.Response(404)

    def client_factory(
        *, runtime_url: str, agent_key: str, control_contract: str | None = None
    ) -> NormaClient:
        return NormaClient(
            runtime_url=runtime_url,
            agent_key=agent_key,
            control_contract=control_contract,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("norma.instrumentation.NormaClient", client_factory)
    monkeypatch.setattr("norma._checks.NormaClient", client_factory)

    async def run() -> object:
        if sync_invocation:
            return agent("hello", invocation_state={"context": {"framework": "provided"}})
        return await agent.invoke_async(
            "hello", invocation_state={"context": {"framework": "provided"}}
        )

    app.run = run
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    "target": "app.run",
                    "context": {"value": {"configured": "value"}},
                    "target_scope": {
                        "type": {"value": "workspace"},
                        "id": {"value": "tenant-1"},
                    },
                }
            ],
            "checkpoints": {},
            "adapters": {
                "native": {
                    "kind": "strands",
                    "agent": {"binding": "native.agent"},
                    "enable_logging": False,
                }
            },
        },
        agent_name="agent-alpha",
        bindings={"app": app, "native": SimpleNamespace(agent=agent)},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    selected_session.append(configured._sessions["agent-alpha"])
    try:
        if invalidate_session_during_check:
            with pytest.raises(RuntimeError, match="session changed during the check"):
                await app.run()
            assert model_calls == 0
        else:
            result = await app.run()
            assert str(result).strip() == "stubbed"
            assert model_calls > 0
        assert checks
        assert all(
            (target_type, target_id) == ("workspace", "tenant-1")
            for _, target_type, target_id in checks
        ), checks
        if not invalidate_session_during_check:
            assert any(
                context.get("configured") == "value" and context.get("framework") == "provided"
                for context, _, _ in checks
            ), checks
    finally:
        await configured.aclose()
