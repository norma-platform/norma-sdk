# Modified for Norma.
"""Exercise actual hosted transport and checkpoint execution without a server."""

import json
from unittest.mock import patch

import httpx
import pytest

import norma
from norma import checks
from norma._checks import request_remote_check
from norma._state import state
from norma.client import NormaClient
from norma.configuration import resolve_url
from norma.models import Agent, Step

NAME = "hosted-test-agent"
URL = "https://runtime.example.test/api/runtime"
SAFE = {"allowed": True, "confidence": 1.0, "execution_contract": "hosted-v1"}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com?q=1",
        "ftp://localhost",
        "https://example.com:invalid",
        "https://example.com:65536",
        "https://example.com:-1",
        "",
    ],
)
def test_invalid_urls_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        resolve_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://runtime.example.test/api/runtime",
        "http://localhost:18000",
        "http://127.0.0.1:18000",
        "http://[::1]:18000",
    ],
)
def test_https_or_loopback_urls(url: str) -> None:
    assert resolve_url(url) == url


@pytest.mark.parametrize("name", ["NORMA_AGENT_KEY", "NORMA_RUNTIME_URL"])
def test_connection_settings_required(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(ValueError, match=name):
        NormaClient()


async def test_wire_identity_credentials_and_gateway_prefix() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SAFE)

    async with NormaClient(
        URL, agent_key="scoped-key", transport=httpx.MockTransport(handler)
    ) as client:
        result = await checks.check(client, NAME, Step(type="tool", name="read", input={}), "pre")
    assert result.allowed
    assert len(requests) == 1
    assert requests[0].url.path == "/api/runtime/api/v1/checks"
    assert requests[0].headers["X-API-Key"] == "scoped-key"
    payload = json.loads(requests[0].content)
    assert payload["agent_name"] == NAME
    assert payload["execution_mode"] == "hosted"
    assert "tenant_id" not in payload


@pytest.mark.parametrize(
    "headers,payload,accepted",
    [
        (
            {"X-Norma-Control-Contract": "rule-controls-v1"},
            {**SAFE, "control_contract": "rule-controls-v1"},
            True,
        ),
        ({}, {**SAFE, "control_contract": "rule-controls-v1"}, False),
        ({"X-Norma-Control-Contract": "rule-controls-v1"}, SAFE, False),
        (
            {"X-Norma-Control-Contract": "rule-controls-v1"},
            {**SAFE, "control_contract": "other"},
            False,
        ),
        (
            {"X-Norma-Control-Contract": "other"},
            {**SAFE, "control_contract": "rule-controls-v1"},
            False,
        ),
    ],
)
async def test_control_contract_opt_in_requires_header_and_envelope(
    headers: dict[str, str], payload: dict[str, object], accepted: bool
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload, headers=headers)

    async with NormaClient(
        URL,
        control_contract="rule-controls-v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        if accepted:
            result = await checks.check(
                client, NAME, Step(type="tool", name="read", input={}), "pre"
            )
            assert result.allowed
        else:
            with pytest.raises(RuntimeError, match="control contract"):
                await checks.check(client, NAME, Step(type="tool", name="read", input={}), "pre")

    assert len(seen) == 1
    assert seen[0].headers["X-Norma-Control-Contract"] == "rule-controls-v1"


async def test_control_contract_is_opt_in_and_legacy_check_sends_no_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SAFE)

    async with NormaClient(URL, transport=httpx.MockTransport(handler)) as client:
        result = await checks.check(client, NAME, Step(type="tool", name="read", input={}), "pre")
    assert result.allowed
    assert len(seen) == 1
    assert "X-Norma-Control-Contract" not in seen[0].headers


async def test_redirect_and_cross_origin_never_receive_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://other.example/steal"})

    async with NormaClient(URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.http_client.get("/health")
        with pytest.raises(ValueError):
            await client.http_client.get("https://other.example/steal")
    assert len(seen) == 1
    assert seen[0].url.host == "runtime.example.test"


@pytest.mark.parametrize(
    "payload",
    [
        {"allowed": True, "confidence": 1},
        {**SAFE, "execution_contract": "hosted-v2"},
        {**SAFE, "allowed": "true"},
        {"execution_contract": "hosted-v1"},
        [],
    ],
)
async def test_unsupported_or_malformed_decisions_fail_closed(payload: object) -> None:
    async with NormaClient(
        URL, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises((RuntimeError, ValueError)):
            await checks.check(client, NAME, Step(type="tool", name="read", input={}), "pre")


async def test_cached_sdk_local_rule_rejected_without_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=SAFE)

    async with NormaClient(URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="SDK-local"):
            await request_remote_check(
                client,
                NAME,
                Step(type="tool", name="read", input={}),
                "pre",
                [{"id": 1, "name": "local", "rule": {"execution": "sdk"}}],
            )
    assert calls == 0


@pytest.mark.parametrize("failure_stage", ["pre", "post"])
@pytest.mark.parametrize("mode", ["network", "auth", "invalid", "unsupported", "deny", "error"])
async def test_checkpoint_failures_never_duplicate_user_action(
    failure_stage: str, mode: str
) -> None:
    effects: list[str] = []
    requests: list[str] = []
    state.current_agent = Agent(agent_name=NAME)
    state.server_url = URL
    state.api_key = "scoped-key"
    state.server_rules = []
    state.rule_cache_healthy = True

    def handler(request: httpx.Request) -> httpx.Response:
        stage = json.loads(request.content)["stage"]
        requests.append(stage)
        if stage != failure_stage:
            return httpx.Response(200, json=SAFE)
        if mode == "network":
            raise httpx.ConnectError("offline", request=request)
        if mode == "auth":
            return httpx.Response(403, json={"detail": "denied"})
        if mode == "invalid":
            return httpx.Response(200, json={**SAFE, "allowed": "true"})
        if mode == "unsupported":
            return httpx.Response(200, json={"allowed": True, "confidence": 1})
        if mode == "deny":
            return httpx.Response(200, json={**SAFE, "allowed": False})
        return httpx.Response(
            200,
            json={
                **SAFE,
                "errors": [
                    {
                        "rule_id": 1,
                        "rule_name": "rule",
                        "action": "deny",
                        "result": {"matched": False, "confidence": 0, "error": "unavailable"},
                    }
                ],
            },
        )

    def client_factory(**kwargs: object) -> NormaClient:
        return NormaClient(URL, agent_key="scoped-key", transport=httpx.MockTransport(handler))

    @norma.checkpoint(step_name="write", step_type="tool")
    async def action(value: str) -> str:
        effects.append(value)
        return value

    with patch("norma.checkpoints.NormaClient", side_effect=client_factory):
        with pytest.raises((norma.CheckError, norma.RuleViolationError)):
            await action("once")
    assert effects == ([] if failure_stage == "pre" else ["once"])
    assert requests == (["pre"] if failure_stage == "pre" else ["pre", "post"])


async def test_empty_rules_still_requires_remote_decision() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SAFE)

    async with NormaClient(URL, transport=httpx.MockTransport(handler)) as client:
        result = await request_remote_check(
            client, NAME, Step(type="tool", name="read", input={}), "pre", []
        )
    assert result.allowed and len(seen) == 1


async def test_checkpoint_unresolved_deny_without_override_blocks_action() -> None:
    # Given: an unresolved receipt retains its configured deny action.
    state.current_agent = Agent(agent_name=NAME)
    state.server_url = URL
    state.api_key = "scoped-key"
    state.server_rules = []
    state.rule_cache_healthy = True
    effects: list[str] = []
    payload = {
        **SAFE,
        "unresolved": [
            {
                "rule_id": 1,
                "rule_name": "evidence",
                "action": "deny",
                "result": {"matched": False, "confidence": 0},
            }
        ],
    }

    def client_factory(**kwargs: object) -> NormaClient:
        return NormaClient(
            URL,
            agent_key="scoped-key",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        )

    @norma.checkpoint(step_name="write", step_type="tool")
    async def action(value: str) -> str:
        effects.append(value)
        return value

    # When: checking the proposed action.
    with patch("norma.checkpoints.NormaClient", side_effect=client_factory):
        with pytest.raises(norma.RuleViolationError):
            await action("once")
    # Then: a missing effective-action override never changes deny to observe.
    assert effects == []


@pytest.mark.parametrize("bucket", ["matches", "non_matches", "unresolved"])
async def test_checkpoint_blocks_evaluator_error_in_every_response_bucket(bucket: str) -> None:
    # Given: a misleading allowed aggregate with a failed individual check.
    state.current_agent = Agent(agent_name=NAME)
    state.server_url = URL
    state.api_key = "scoped-key"
    state.server_rules = []
    state.rule_cache_healthy = True
    effects = []
    payload = {
        **SAFE,
        bucket: [
            {
                "rule_id": 1,
                "rule_name": "broken",
                "action": "observe",
                "result": {
                    "matched": False,
                    "confidence": 0,
                    "error": "failed",
                },
            }
        ],
    }

    def client_factory(**kwargs: object) -> NormaClient:
        return NormaClient(
            URL,
            agent_key="scoped-key",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=payload),
            ),
        )

    @norma.checkpoint()
    async def action() -> None:
        effects.append("ran")

    # When: executing through the public checkpoint using the real wire parser.
    with patch("norma.checkpoints.NormaClient", side_effect=client_factory):
        with pytest.raises(norma.CheckError):
            await action()
    # Then: no bucket placement can turn an evaluator failure into permission.
    assert effects == []
