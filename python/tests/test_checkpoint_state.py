# Modified for Norma.
"""Public checkpoint state contracts over the real hosted wire with mocked transport."""

import asyncio
import json
from collections.abc import Iterator
from copy import deepcopy
from unittest.mock import patch

import httpx
import pytest

import norma
from norma._state import state as session
from norma.client import NormaClient
from norma.models import Agent

SAFE = {"allowed": True, "confidence": 1.0, "execution_contract": "hosted-v1"}
URL = "https://runtime.example.test/api/runtime"


@pytest.fixture
def captured_checks() -> Iterator[list[dict]]:
    # Given: private session setup avoids initialization; requests still use real wire code.
    session.current_agent = Agent(agent_name="state-test")
    session.server_url = URL
    session.api_key = "synthetic-agent-key"
    session.server_rules = []
    session.rule_cache_healthy = True
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=SAFE)

    def client_factory(**kwargs: object) -> NormaClient:
        return NormaClient(
            URL, agent_key="synthetic-agent-key", transport=httpx.MockTransport(handler)
        )

    with patch("norma.checkpoints.NormaClient", side_effect=client_factory):
        yield captured


async def test_state_is_snapshotted_once_for_pre_and_post(captured_checks: list[dict]) -> None:
    # Given: a caller-owned state object changed by the action.
    observed = {"inventory": {"available": 8}}

    @norma.checkpoint(state=observed)
    async def reserve(quantity: int) -> int:
        observed["inventory"]["available"] = 1
        return quantity

    # When: invoking twice, each invocation observes its own boundary.
    assert await reserve(2) == 2
    assert await reserve(3) == 3

    # Then: pre and post share the original observation, later calls see current values.
    assert [r["step"]["context"]["state"] for r in captured_checks] == [
        {"inventory": {"available": 8}},
        {"inventory": {"available": 8}},
        {"inventory": {"available": 1}},
        {"inventory": {"available": 1}},
    ]
    assert [r["step"]["input"] for r in captured_checks] == [
        {"quantity": 2},
        {"quantity": 2},
        {"quantity": 3},
        {"quantity": 3},
    ]
    assert all("norma_action" in r["step"]["context"] for r in captured_checks)


async def test_async_provider_is_invocation_local(captured_checks: list[dict]) -> None:
    # Given: simultaneous users of one agent interleave at the state provider.
    entered = 0
    ready = asyncio.Event()
    calls: list[str] = []

    async def observed(user: str, quantity: int) -> dict:
        nonlocal entered
        entered += 1
        calls.append(user)
        if entered == 2:
            ready.set()
        await ready.wait()
        return {"owner": user, "available": quantity + 10}

    @norma.checkpoint(state=observed)
    async def reserve(user: str, quantity: int) -> str:
        await asyncio.sleep(0)
        return user

    # When: both invocations run concurrently.
    assert await asyncio.gather(reserve("a", 1), reserve("b", 2)) == ["a", "b"]

    # Then: neither invocation inherits the other's state, and providers run once.
    assert sorted(calls) == ["a", "b"]
    assert len(captured_checks) == 4
    for request in captured_checks:
        values = request["step"]["input"]
        assert request["step"]["context"]["state"] == {
            "owner": values["user"],
            "available": values["quantity"] + 10,
        }


async def test_provider_mutation_cannot_change_checked_arguments(
    captured_checks: list[dict],
) -> None:
    # Given: a provider mistakenly mutates its argument copy.
    value = {"quantity": 2}
    seen = []

    def observed(request: dict) -> dict:
        request["quantity"] = 500
        return {"available": 8}

    @norma.checkpoint(state=observed)
    async def reserve(request: dict) -> None:
        seen.append(deepcopy(request))
        request["quantity"] = 9

    # When: the checkpoint executes.
    await reserve(value)

    # Then: caller, effect, and recorded input retain the intended argument.
    assert value == {"quantity": 2}
    assert seen == [{"quantity": 2}]
    assert [r["step"]["input"] for r in captured_checks] == [
        {"request": {"quantity": 2}},
        {"request": {"quantity": 2}},
    ]


@pytest.mark.parametrize("async_provider", [False, True])
async def test_provider_failure_is_redacted_and_blocks_action(
    captured_checks: list[dict],
    async_provider: bool,
) -> None:
    # Given: an application callback fails with sensitive text.
    effects = []

    def fail() -> dict:
        raise ValueError("secret-do-not-return")

    async def async_fail() -> dict:
        return fail()

    @norma.checkpoint(state=async_fail if async_provider else fail)
    async def action() -> None:
        effects.append(True)

    # When: capturing the snapshot fails.
    with pytest.raises(norma.CheckError, match="state could not be captured") as caught:
        await action()

    # Then: no check or effect runs and the public message contains no provider detail.
    assert caught.value.stage == "pre"
    assert "secret" not in str(caught.value)
    assert effects == []
    assert captured_checks == []


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"x": float("nan")},
        {"x": float("inf")},
        {1: "x"},
        {"x": object()},
        {"x": (1, 2)},
        {"x": "a" * 65_536},
        {"x": "😀" * 20_000},
        {"x": [0] * 10_001},
        {"x": 2**53},
        {"x": float(2**53)},
        {"x": -0.0},
    ],
)
async def test_non_json_or_oversized_state_blocks_effect(
    captured_checks: list[dict], bad: object
) -> None:
    # Given: invalid application state, including values that ordinary JSON encoders coerce.
    effects = []

    @norma.checkpoint(state=bad)
    async def action() -> None:
        effects.append(True)

    # When: invoking the public checkpoint.
    with pytest.raises(norma.CheckError):
        await action()

    # Then: invalid observations never reach the runtime or the effect.
    assert captured_checks == []
    assert effects == []


@pytest.mark.parametrize("cyclic", [False, True])
async def test_state_depth_and_cycles_are_bounded(
    captured_checks: list[dict], cyclic: bool
) -> None:
    # Given: nested or cyclic state.
    observed: dict = {}
    cursor = observed
    for _ in range(35):
        child: dict = {}
        cursor["nested"] = child
        cursor = child
    if cyclic:
        observed["cycle"] = observed

    @norma.checkpoint(state=observed)
    async def action() -> None:
        pytest.fail("effect must be blocked")

    # When / Then: validation fails before any remote check.
    with pytest.raises(norma.CheckError):
        await action()
    assert captured_checks == []


def test_sync_action_accepts_async_provider(captured_checks: list[dict]) -> None:
    # Given: synchronous checkpoint calling a coroutine state provider.
    async def observed(quantity: int) -> dict:
        return {"available": quantity + 1}

    @norma.checkpoint(state=observed)
    def reserve(quantity: int) -> int:
        return quantity

    # When: invoking synchronously.
    assert reserve(2) == 2

    # Then: the awaited snapshot appears on both stages.
    assert [r["step"]["context"]["state"] for r in captured_checks] == [
        {"available": 3},
        {"available": 3},
    ]


async def test_no_state_remains_unchanged_and_empty_state_is_explicit(
    captured_checks: list[dict],
) -> None:
    # Given: old checkpoints and an explicitly empty observation.
    @norma.checkpoint()
    async def old() -> None:
        pass

    @norma.checkpoint(state={})
    async def empty() -> None:
        pass

    # When: both execute.
    await old()
    await empty()

    # Then: old payloads do not gain a state field.
    assert all("state" not in r["step"]["context"] for r in captured_checks[:2])
    assert all(r["step"]["context"]["state"] == {} for r in captured_checks[2:])


@pytest.mark.parametrize("mode", ["deny", "steer", "observe", "error", "unavailable"])
async def test_state_preserves_hosted_enforcement(captured_checks: list[dict], mode: str) -> None:
    # Given: a state-bearing checkpoint with a real hosted response parser.
    effects: list[bool] = []
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if mode == "unavailable":
            raise httpx.ConnectError("offline", request=request)
        match = {
            "rule_id": 1,
            "rule_name": "inventory",
            "action": "observe" if mode == "error" else mode,
            "result": {"matched": mode != "error", "confidence": 1},
        }
        bucket = "matches"
        if mode == "error":
            match["result"]["error"] = "invalid observed value"
            bucket = "errors"
        return httpx.Response(
            200,
            json={**SAFE, "allowed": mode not in {"deny", "steer"}, bucket: [match]},
        )

    def client_factory(**kwargs: object) -> NormaClient:
        return NormaClient(URL, transport=httpx.MockTransport(handler))

    @norma.checkpoint(state={"available": 4})
    async def action() -> None:
        effects.append(True)

    # When: the runtime matches a Rule, returns an error, or is unavailable.
    with patch("norma.checkpoints.NormaClient", side_effect=client_factory):
        if mode == "observe":
            await action()
        else:
            error = {
                "deny": norma.RuleViolationError,
                "steer": norma.RuleSteerError,
                "error": norma.CheckError,
                "unavailable": norma.CheckError,
            }[mode]
            with pytest.raises(error):
                await action()

    # Then: only observe permits the effect, and all attempted checks carry the snapshot.
    assert effects == ([True] if mode == "observe" else [])
    assert len(requests) == (2 if mode == "observe" else 1)
    assert all(r["step"]["context"]["state"] == {"available": 4} for r in requests)


async def test_caller_mutation_during_check_does_not_change_state(
    captured_checks: list[dict],
) -> None:
    # Given: a transport that yields while the caller mutates the original object.
    observed = {"available": 12}
    entered = asyncio.Event()
    resume = asyncio.Event()
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            entered.set()
            await resume.wait()
        return httpx.Response(200, json=SAFE)

    def client_factory(**kwargs: object) -> NormaClient:
        return NormaClient(URL, transport=httpx.MockTransport(handler))

    @norma.checkpoint(state=observed)
    async def action() -> None:
        pass

    # When: the caller mutates state while preflight is awaiting a response.
    with patch("norma.checkpoints.NormaClient", side_effect=client_factory):
        pending = asyncio.create_task(action())
        await entered.wait()
        observed["available"] = 0
        resume.set()
        await pending

    # Then: the action's postcheck still refers to its original observation.
    assert [r["step"]["context"]["state"] for r in requests] == [
        {"available": 12},
        {"available": 12},
    ]


async def test_provider_receives_defaults_and_keyword_only_arguments(
    captured_checks: list[dict],
) -> None:
    # Given: a checkpoint with ordinary, defaulted, and keyword-only arguments.
    seen: list[tuple[str, int, bool]] = []

    def observed(order: str, quantity: int = 1, *, priority: bool = False) -> dict:
        seen.append((order, quantity, priority))
        return {"available": 5}

    @norma.checkpoint(state=observed)
    async def reserve(order: str, quantity: int = 1, *, priority: bool = False) -> str:
        return order

    # When: supplying a keyword-only override while omitting a defaulted argument.
    assert await reserve(order="order-a", priority=True) == "order-a"

    # Then: the provider is called once with the same effective arguments.
    assert seen == [("order-a", 1, True)]
    assert captured_checks[0]["step"]["input"] == {
        "order": "order-a",
        "quantity": 1,
        "priority": True,
    }


@pytest.mark.parametrize("step_type", ["tool", "retriever"])
async def test_named_argument_shape_preserves_parameter_named_args(
    captured_checks: list[dict], step_type: str
) -> None:
    # Given: an argument named args must not be mistaken for TS positional input.args.
    @norma.checkpoint(step_type=step_type, state={"available": 5})
    async def action(args: list[int]) -> list[int]:
        return args

    # When: the action passes through pre and post checks.
    assert await action([1, 2]) == [1, 2]

    # Then: payload shape is unchanged and both stages explicitly identify named arguments.
    assert [request["step"]["input"] for request in captured_checks] == [
        {"args": [1, 2]},
        {"args": [1, 2]},
    ]
    assert all(
        request["step"]["context"]["norma_argument_shape"] == "named"
        and request["step"]["context"]["state"] == {"available": 5}
        for request in captured_checks
    )
    if step_type == "tool":
        assert all("norma_action" in request["step"]["context"] for request in captured_checks)


async def test_llm_prompt_is_not_mislabeled_as_named_arguments(captured_checks: list[dict]) -> None:
    # Given: LLM checkpoints preserve their existing extracted prompt payload.
    @norma.checkpoint(step_type="llm", state={"topic": "public"})
    async def action(prompt: str) -> str:
        return prompt

    # When: executing the LLM checkpoint.
    assert await action("hello") == "hello"

    # Then: arbitrary prompt content is not advertised as a named-argument mapping.
    assert all(request["step"]["input"] == "hello" for request in captured_checks)
    assert all(
        "norma_argument_shape" not in request["step"]["context"] for request in captured_checks
    )


async def test_documented_shared_boundary_uses_trusted_facts(captured_checks, monkeypatch):
    """Execute the canonical snippet with application storage mocked, no provider."""
    import re
    import sys
    import types
    from pathlib import Path

    page = Path(__file__).resolve().parents[3] / "ui/public/docs/input-contracts.md"
    if not page.exists():
        pytest.skip("Canonical Console docs are not included in standalone SDK exports")
    source = re.findall(r"```python\n(.*?)```", page.read_text(), re.S)[0]
    storage = types.ModuleType("document_service")
    facts = {"used_bytes": 4, "quota_bytes": 20, "private_note": "never transmit"}
    effects = []

    async def authorized_usage(document_id):
        assert document_id == "synthetic-document"
        return facts

    async def write_document(document_id, content):
        effects.append((document_id, content))
        return "stored"

    storage.authorized_usage = authorized_usage
    storage.write_document = write_document
    monkeypatch.setitem(sys.modules, "document_service", storage)
    namespace = {}
    exec(compile(source, str(page), "exec"), namespace)
    assert await namespace["store_document"]("synthetic-document", "é") == "stored"
    assert effects == [("synthetic-document", "é")]
    assert [item["step"]["input"] for item in captured_checks] == [
        {"document_id": "synthetic-document", "content": "é"}
    ] * 2
    assert [item["step"]["context"]["state"] for item in captured_checks] == [
        {"used_bytes": 4, "quota_bytes": 20, "bytes_needed": 2}
    ] * 2

    # Missing or incorrectly typed facts cannot become a successful write.
    for invalid in ({}, {"used_bytes": True, "quota_bytes": 20},
                    {"used_bytes": 1, "quota_bytes": "20"}):
        facts = invalid
        with pytest.raises(norma.CheckError, match="state could not be captured"):
            await namespace["store_document"]("synthetic-document", "more")
    assert effects == [("synthetic-document", "é")]
    assert len(captured_checks) == 2

    # The actual optional framework decorator exposes only proposal arguments.
    ToolContext = pytest.importorskip("agents.tool_context").ToolContext
    tool_source = re.findall(r"```python\n(.*?)```", page.read_text(), re.S)[1]
    exec(compile(tool_source, str(page), "exec"), namespace)
    tool = namespace["store_document_tool"]
    assert set(tool.params_json_schema["properties"]) == {"document_id", "content"}
    facts = {"used_bytes": 4, "quota_bytes": 20}
    assert await tool.on_invoke_tool(
        ToolContext(
            context=None, tool_name=tool.name,
            tool_call_id="synthetic-call", tool_arguments="{}",
        ),
        json.dumps({"document_id": "synthetic-document", "content": "tool"}),
    ) == "stored"
    assert effects[-1] == ("synthetic-document", "tool")
    assert len(captured_checks) == 4
