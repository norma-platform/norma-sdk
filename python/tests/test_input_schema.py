from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import norma
from norma.client import NormaClient

URL = "https://runtime.example.test/api/runtime"
SAFE = {"allowed": True, "confidence": 1.0, "execution_contract": "hosted-v1"}
_INPUT_SCHEMA_FIXTURE = (
    Path(__file__).resolve().parents[2] / "schema/validation-fixtures/input-schema.json"
)
_INPUT_SCHEMA_CASES = json.loads(_INPUT_SCHEMA_FIXTURE.read_text())["cases"]


def _config(schema: object, *, input_from: object | None = None) -> dict[str, object]:
    checkpoint: dict[str, object] = {
        "target": "app.send",
        "step_type": "tool",
        "input_schema": schema,
    }
    if input_from is not None:
        checkpoint["input_from"] = input_from
    return {"version": 2, "entrypoints": ["app.run"], "checkpoints": {"send": checkpoint}}


@pytest.mark.parametrize("case", _INPUT_SCHEMA_CASES, ids=lambda item: item["name"])
def test_input_schema_shared_parser_and_offline_cli_fixture(
    tmp_path: Path, case: dict[str, object]
) -> None:
    source = str(case["source"])
    if case["parse_valid"]:
        norma.parse_instrumentation(source, format=str(case["format"]))
    else:
        with pytest.raises(ValueError):
            norma.parse_instrumentation(source, format=str(case["format"]))

    config = tmp_path / "agent-alpha.json"
    config.write_text(source, encoding="utf-8")
    cli = Path(sys.executable).with_name("norma")
    result = subprocess.run(
        [str(cli), "config", "validate", str(config), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    expected_valid = bool(case["config_valid"])
    assert result.returncode == (0 if expected_valid else 1)
    report = json.loads(result.stdout)
    assert report["valid"] is expected_valid
    if "config_path" in case:
        assert report["diagnostics"][0]["path"] == case["config_path"]
    literal_check = case.get("literal_check")
    if isinstance(literal_check, dict) and not literal_check["valid"]:
        diagnostic = next(
            item for item in report["diagnostics"] if item["path"] == literal_check["config_path"]
        )
        assert literal_check["instance_path"] in diagnostic["message"]


@pytest.mark.asyncio
async def test_input_schema_blocks_missing_required_before_check_or_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace()
    calls: list[str] = []
    checks: list[dict[str, object]] = []

    async def send(recipient: str = "default", topic: str | None = None) -> None:
        calls.append("action")

    async def run() -> None:
        await app.send(topic="hello")

    app.send, app.run = send, run

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
    schema = {
        "type": "object",
        "required": ["recipient"],
        "properties": {"recipient": {"type": "string", "default": "schema-default"}},
    }
    configured = await norma.aconfigure(
        _config(schema, input_from={"path": ["kwargs"]}),
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
        with pytest.raises(norma.CheckError) as error:
            await app.run()
        assert "send" in str(error.value)
        assert "/checkpoints/send/input_schema" in str(error.value)
        assert "/recipient" in str(error.value)
        assert "default" not in str(error.value)
        assert calls == []
        assert checks == []
    finally:
        await configured.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected_recipient"),
    [({"recipient": "alice"}, "alice"), ({"recipient": None}, None), ({}, "default")],
)
async def test_input_schema_validates_final_input_without_mutating_defaults(
    monkeypatch: pytest.MonkeyPatch,
    value: dict[str, object],
    expected_recipient: str | None,
) -> None:
    app = SimpleNamespace()
    actions: list[tuple[str | None, str | None]] = []
    checks: list[dict[str, object]] = []

    async def send(recipient: str | None = "default", topic: str | None = None) -> None:
        actions.append((recipient, topic))

    async def run() -> None:
        await app.send(**value)

    app.send, app.run = send, run

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
    schema = {
        "type": "object",
        "required": ["recipient"],
        "properties": {"recipient": {"type": ["string", "null"]}},
    }
    configured = await norma.aconfigure(
        _config(schema),
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
        await app.run()
        assert actions == [(expected_recipient, None)]
        assert len(checks) == 2
        assert all(
            check["step"]["input"] == {"recipient": expected_recipient, "topic": None}
            for check in checks
        )
    finally:
        await configured.aclose()


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "unknown_constraint": True},
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"},
        {"$ref": "https://example.invalid/schema.json"},
        {"$ref": "#/definitions/missing"},
        {"$ref": "#/const/$ref", "const": {"$ref": "https://example.invalid/data"}},
        {"type": "object", "properties": {"child": {"$ref": "#"}}},
        {"properties": {"nested": {"$schema": "https://json-schema.org/draft/2020-12/schema"}}},
        {"properties": {"nested": {"$id": "https://example.invalid/schema"}}},
        {
            "definitions": {"a": {"$ref": "#/definitions/b"}, "b": {"$ref": "#/definitions/a"}},
            "$ref": "#/definitions/a",
        },
    ],
)
def test_parse_rejects_unsupported_or_unresolvable_input_schema(schema: object) -> None:
    with pytest.raises(ValueError, match="input_schema"):
        norma.parse_instrumentation(json.dumps(_config(schema)))


def test_parse_rejects_literal_input_that_does_not_match_schema() -> None:
    config = _config(
        {"type": "object", "required": ["recipient"]},
        input_from={"value": {"topic": "x"}},
    )
    with pytest.raises(ValueError, match="input_schema"):
        norma.parse_instrumentation(json.dumps(config))


def test_schema_literal_data_is_not_walked_as_schema_keywords() -> None:
    schema = {
        "type": "object",
        "const": {"$ref": "https://example.invalid/is-data", "custom": "literal"},
        "properties": {"$ref": {"type": "string"}},
    }
    parsed = norma.parse_instrumentation(json.dumps(_config(schema)))
    assert parsed["checkpoints"]["send"]["input_schema"] == schema


def test_schema_reference_supports_escaped_json_pointer_tokens(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "required": ["recipient"],
        "properties": {"recipient": {"$ref": "#/definitions/a~1b"}},
        "definitions": {"a/b": {"type": "string"}},
    }
    config = _config(schema)
    norma.parse_instrumentation(json.dumps(config))
    config_path = tmp_path / "agent-alpha.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "condition": {
                            "selector": {"path": "input.recipient"},
                            "evaluator": {"name": "json", "config": {}},
                        },
                        "scope": {"stages": ["pre"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("norma")),
            "config",
            "check",
            str(config_path),
            "--rules",
            str(rules_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["coverage"][0]["status"] == "covered"


def test_external_schema_references_never_attempt_network_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("input schema attempted external retrieval")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    monkeypatch.setattr("socket.create_connection", fail)
    with pytest.raises(ValueError, match="input_schema"):
        norma.parse_instrumentation(json.dumps(_config({"$ref": "https://example.invalid/x"})))


def test_config_check_never_derives_coverage_from_invalid_input_contract(tmp_path: Path) -> None:
    config = tmp_path / "agent-alpha.json"
    config.write_text(json.dumps(_config({"type": "object", "format": "email"})))
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "condition": {
                            "selector": {"path": "input.recipient"},
                            "evaluator": {"name": "json", "config": {}},
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    cli = Path(sys.executable).with_name("norma")
    result = subprocess.run(
        [str(cli), "config", "check", str(config), "--rules", str(rules), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["coverage"] == []
    assert any(
        item["code"] == "schema" and item["path"] == "/checkpoints/send/input_schema"
        for item in report["diagnostics"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [item for item in _INPUT_SCHEMA_CASES if "runtime_input_check" in item],
    ids=lambda item: item["name"],
)
async def test_input_schema_runtime_shared_fixture(
    monkeypatch: pytest.MonkeyPatch, case: dict[str, object]
) -> None:
    runtime_case = case["runtime_input_check"]
    assert isinstance(runtime_case, dict)
    config = json.loads(str(case["source"]))
    checkpoint = config["checkpoints"]["send"]
    checkpoint["input_from"] = {"path": ["kwargs"]}
    app = SimpleNamespace()
    action_calls: list[dict[str, object]] = []
    check_calls: list[dict[str, object]] = []
    value = runtime_case["input"]

    async def send(**kwargs: object) -> object:
        action_calls.append(kwargs)
        return kwargs

    async def run() -> object:
        return await app.send(**value)

    app.send, app.run = send, run

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent_name": "agent-alpha"})
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if request.url.path.endswith("/checks"):
            check_calls.append(json.loads(request.content))
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
    try:
        if runtime_case["valid"]:
            await app.run()
            assert action_calls == [value]
            assert len(check_calls) == 2
        else:
            with pytest.raises(norma.CheckError) as error:
                await app.run()
            assert action_calls == []
            assert check_calls == []
            assert runtime_case["config_path"] in str(error.value)
            assert runtime_case["instance_path"] in str(error.value)
    finally:
        await configured.aclose()


def test_offline_validate_reports_schema_pointer_without_rejected_value(tmp_path: Path) -> None:
    config = _config({"type": "object", "unknown_constraint": "secret-value"})
    path = tmp_path / "agent-alpha.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    cli = Path(sys.executable).parent / "norma"
    result = subprocess.run(
        [str(cli), "config", "validate", str(path), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["diagnostics"][0]["path"] == "/checkpoints/send/input_schema"
    assert "secret-value" not in report["diagnostics"][0]["message"]


@pytest.mark.parametrize(
    ("schema", "selector", "required_fields", "input_from", "expected"),
    [
        (
            {
                "type": "object",
                "required": ["recipient"],
                "properties": {"recipient": {"type": "string"}},
            },
            "input.recipient",
            None,
            None,
            "covered",
        ),
        (
            {"type": "object", "properties": {"recipient": {"type": "string"}}},
            "input.recipient",
            None,
            None,
            "unknown",
        ),
        (
            {
                "type": "object",
                "required": ["recipient"],
                "properties": {"recipient": {"type": ["string", "null"]}},
            },
            "input.recipient",
            None,
            None,
            "covered",
        ),
        (
            {
                "type": "object",
                "required": ["recipient"],
                "properties": {"recipient": {"type": ["string", "null"]}},
            },
            "input",
            ["recipient"],
            None,
            "unknown",
        ),
        (
            {
                "type": "object",
                "required": ["recipient"],
                "properties": {"recipient": {"type": "null"}},
            },
            "input",
            ["recipient"],
            None,
            "missing",
        ),
        (
            {"type": "object", "additionalProperties": False},
            "input.unknown",
            None,
            None,
            "missing",
        ),
        (
            {
                "$ref": "#/definitions/body",
                "definitions": {
                    "body": {
                        "type": "object",
                        "required": ["recipient"],
                        "properties": {"recipient": {"type": "string"}},
                    }
                },
            },
            "input.recipient",
            None,
            None,
            "covered",
        ),
        (
            {
                "type": "object",
                "required": ["recipient"],
                "properties": {"recipient": {"type": "string"}},
            },
            "input.subject",
            None,
            {"value": {"recipient": "alice"}},
            "missing",
        ),
        ({}, "input", None, None, "unknown"),
    ],
)
def test_rule_coverage_uses_input_schema_conservatively(
    tmp_path: Path,
    schema: dict[str, object],
    selector: str,
    required_fields: list[str] | None,
    input_from: dict[str, object] | None,
    expected: str,
) -> None:
    checkpoint: dict[str, object] = {"target": "app.send", "input_schema": schema}
    if input_from is not None:
        checkpoint["input_from"] = input_from
    config = {
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {"send": checkpoint},
    }
    evaluator_config: dict[str, object] = (
        {"required_fields": required_fields} if required_fields else {}
    )
    rules = {
        "rules": [
            {
                "condition": {
                    "selector": {"path": selector},
                    "evaluator": {"name": "json", "config": evaluator_config},
                },
                "scope": {"stages": ["pre"]},
            }
        ]
    }
    config_path = tmp_path / "agent-alpha.json"
    rules_path = tmp_path / "rules.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    rules_path.write_text(json.dumps(rules), encoding="utf-8")
    cli = Path(sys.executable).with_name("norma")
    result = subprocess.run(
        [str(cli), "config", "check", str(config_path), "--rules", str(rules_path), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(result.stdout)
    statuses = [item["status"] for item in report["coverage"]]
    if required_fields:
        assert statuses == [expected, "covered"]
    else:
        assert statuses == [expected]
    assert result.returncode == (0 if expected == "covered" else 1)
