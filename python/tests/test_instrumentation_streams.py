# Modified for Norma.
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import norma
from norma.client import NormaClient

URL = "https://runtime.example.test/api/runtime"


def _mock_client(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/agents/agent-alpha"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-alpha"}, "steps": []})
        if path.endswith("/agents/agent-bravo"):
            return httpx.Response(200, json={"agent": {"agent_name": "agent-bravo"}, "steps": []})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
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
    return requests


def _mock_finish_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: str = "pass",
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/checks"):
            body = json.loads(request.content)
            turns.append({"agent_name": body["agent_name"], **body["step"]["input"]})
            return httpx.Response(
                200,
                json={
                    "execution_contract": "hosted-v1",
                    "verdict": verdict,
                    "reason": "Checked",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": {"count": 0, "max": 1 if verdict == "hold" else 0},
                    "allow_finish": verdict != "hold",
                    "outcome": "held" if verdict == "hold" else "completed",
                },
            )
        if request.url.path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if "/agents/agent-" in request.url.path:
            agent_name = request.url.path.rsplit("/", maxsplit=1)[-1]
            return httpx.Response(200, json={"agent": {"agent_name": agent_name}, "steps": []})
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
    return turns


def _stream_config(finish: dict[str, Any]) -> dict[str, object]:
    return {
        "version": 2,
        "entrypoints": [
            {
                "target": "app.stream",
                "turn": {
                    "session_id": {"value": "session-1"},
                    "turn_id": {"value": "turn-1"},
                    "finish": finish,
                },
            }
        ],
        "checkpoints": {},
    }


@pytest.mark.asyncio
async def test_sync_generator_protocol_runs_in_scope_and_restores_consumer_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_client(monkeypatch)
    app = SimpleNamespace()
    cleanup: list[str | None] = []

    def stream() -> Any:
        try:
            received = yield norma.current_agent().agent_name
            try:
                yield received
            except LookupError as error:
                yield str(error)
            return "stream-result"
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    configured = await norma.aconfigure(
        {"version": 2, "entrypoints": ["app.stream"], "checkpoints": {}},
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert next(iterator) == "agent-alpha"
        assert norma.current_agent() is None
        assert iterator.send("sent-value") == "sent-value"
        assert norma.current_agent() is None
        assert iterator.throw(LookupError("injected")) == "injected"
        assert norma.current_agent() is None
        with pytest.raises(StopIteration) as stopped:
            next(iterator)
        assert stopped.value.value == "stream-result"
        assert cleanup == ["agent-alpha"]

        early = app.stream()
        assert next(early) == "agent-alpha"
        await configured.aclose()
        early.close()
        assert cleanup == ["agent-alpha", "agent-alpha"]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_async_generators_scope_each_advance_concurrently_and_allow_cleanup_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_client(monkeypatch)
    app = SimpleNamespace()
    cleanup: list[str | None] = []
    release = asyncio.Event()

    async def alpha_stream() -> Any:
        try:
            yield norma.current_agent().agent_name
            await release.wait()
            yield norma.current_agent().agent_name
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    async def bravo_stream() -> Any:
        try:
            yield norma.current_agent().agent_name
            await release.wait()
            yield norma.current_agent().agent_name
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.alpha_stream = alpha_stream
    app.bravo_stream = bravo_stream
    configs = {
        "agent-alpha": {
            "version": 2,
            "entrypoints": ["app.alpha_stream"],
            "checkpoints": {},
        },
        "agent-bravo": {
            "version": 2,
            "entrypoints": ["app.bravo_stream"],
            "checkpoints": {},
        },
    }
    config_dir = tempfile.TemporaryDirectory()
    directory = Path(config_dir.name)
    for name, config in configs.items():
        (directory / f"{name}.json").write_text(json.dumps(config), encoding="utf-8")
    configured = await norma.aconfigure(
        directory,
        bindings={"app": app},
        connections={
            name: {"runtime_url": URL, "agent_key": f"key-{name}", "observability_enabled": False}
            for name in configs
        },
    )
    try:
        alpha = app.alpha_stream()
        bravo = app.bravo_stream()
        first = await asyncio.gather(alpha.__anext__(), bravo.__anext__())
        assert first == ["agent-alpha", "agent-bravo"]
        assert norma.current_agent() is None
        release.set()
        second = await asyncio.gather(alpha.__anext__(), bravo.__anext__())
        assert second == ["agent-alpha", "agent-bravo"]
        assert norma.current_agent() is None
        await configured.aclose()
        await asyncio.gather(alpha.aclose(), bravo.aclose())
        assert cleanup == ["agent-alpha", "agent-bravo"]
    finally:
        await configured.aclose()
        config_dir.cleanup()


@pytest.mark.asyncio
async def test_cancelled_async_generator_advance_restores_consumer_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_client(monkeypatch)
    app = SimpleNamespace()
    entered = asyncio.Event()
    cleanup: list[str | None] = []

    async def stream() -> Any:
        try:
            yield norma.current_agent().agent_name
            entered.set()
            await asyncio.Event().wait()
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    configured = await norma.aconfigure(
        {"version": 2, "entrypoints": ["app.stream"], "checkpoints": {}},
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert await iterator.__anext__() == "agent-alpha"
        task = asyncio.create_task(iterator.__anext__())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup == ["agent-alpha"]
        assert norma.current_agent() is None
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_async_generator_forwards_send_throw_and_close_after_handle_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_client(monkeypatch)
    app = SimpleNamespace()
    cleanup: list[str | None] = []

    async def stream() -> Any:
        try:
            sent = yield norma.current_agent().agent_name
            yield sent
            try:
                yield "waiting"
            except LookupError as error:
                yield str(error)
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    configured = await norma.aconfigure(
        {"version": 2, "entrypoints": ["app.stream"], "checkpoints": {}},
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    iterator = app.stream()
    assert await iterator.__anext__() == "agent-alpha"
    assert await iterator.asend("sent-value") == "sent-value"
    assert await iterator.__anext__() == "waiting"
    assert await iterator.athrow(LookupError("injected")) == "injected"
    assert norma.current_agent() is None
    await configured.aclose()
    await iterator.aclose()
    assert cleanup == ["agent-alpha"]


@pytest.mark.asyncio
async def test_v1_generator_entrypoint_remains_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _mock_client(monkeypatch)
    app = SimpleNamespace()

    def stream() -> Any:
        yield "item"

    app.stream = stream
    with pytest.raises(ValueError, match="generator"):
        await norma.aconfigure(
            {"version": 1, "entrypoints": ["app.stream"], "checkpoints": {}},
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={"runtime_url": URL, "agent_key": "key-alpha"},
        )
    assert requests == []


@pytest.mark.asyncio
async def test_stream_finish_requires_existing_generator_entrypoint_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _mock_client(monkeypatch)
    app = SimpleNamespace(stream=lambda: iter(["not configured as a generator function"]))
    with pytest.raises(ValueError, match="requires a generator entrypoint"):
        await norma.aconfigure(
            _stream_config({"stream": {}, "on_hold": "raise"}),
            agent_name="agent-alpha",
            bindings={"app": app},
            connection={"runtime_url": URL, "agent_key": "key-alpha"},
        )
    assert requests == []


@pytest.mark.asyncio
async def test_async_stream_finish_collects_chunks_and_checks_only_on_natural_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    callbacks: list[dict[str, object]] = []

    async def stream() -> Any:
        yield "first"
        yield " second"

    app.stream = stream
    app.on_verdict = callbacks.append
    config = {
        "version": 2,
        "entrypoints": [
            {
                "target": "app.stream",
                "turn": {
                    "session_id": {"value": "session-1"},
                    "turn_id": {"value": "turn-1"},
                    "finish": {
                        "stream": {},
                        "on_hold": "raise",
                        "on_verdict": {"binding": "app.on_verdict"},
                    },
                },
            }
        ],
        "checkpoints": {},
    }
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert await iterator.__anext__() == "first"
        assert turns == []
        assert await iterator.__anext__() == " second"
        assert turns == []
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()
        assert [turn["final_message"] for turn in turns] == ["first second"]
        assert len(callbacks) == 1
        assert callbacks[0]["result"] == "first second"
        assert callbacks[0]["verdict"].verdict == "pass"
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_async_stream_finish_preserves_acknowledged_generation_and_control_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma.client import NormaClient

    generation = "stream-generation-1"
    checks: list[dict[str, Any]] = []
    callbacks: list[dict[str, object]] = []

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
            checks.append(body)
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
                            "definition_fingerprint": "b" * 64,
                            "references": [],
                        }
                    ],
                    "verdict": "pass",
                    "reason": "Accepted",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": {
                        "count": 0,
                        "max": 2,
                        "agent_generation": generation,
                    },
                    "allow_finish": True,
                    "outcome": "completed",
                    "provider_input_tokens": None,
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
    app = SimpleNamespace()

    async def stream() -> Any:
        yield "streamed result"

    app.stream = stream
    app.on_verdict = callbacks.append
    config = _stream_config(
        {
            "stream": {},
            "on_hold": "raise",
            "on_verdict": {"binding": "app.on_verdict"},
        }
    )
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={
            "runtime_url": URL,
            "agent_key": "key-alpha",
            "control_contract": "rule-controls-v1",
            "observability_enabled": False,
        },
    )
    try:
        iterator = app.stream()
        assert await iterator.__anext__() == "streamed result"
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()
        assert len(checks) == 1
        assert checks[0]["step"]["input"]["continuation"] == {"count": 0, "max": 2}
        verdict = callbacks[0]["verdict"]
        assert verdict.verdict == "pass"
        assert verdict.continuation.agent_generation == generation
        assert verdict.control_evidence[0].rule_id == 17
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_sync_stream_finish_preserves_generator_return_and_does_not_collect_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    cleanup: list[str | None] = []

    def stream() -> Any:
        try:
            yield "one"
            sent = yield "two"
            yield sent
            return "return-is-not-stream-text"
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    configured = await norma.aconfigure(
        _stream_config({"stream": {}, "on_hold": "raise"}),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert next(iterator) == "one"
        assert next(iterator) == "two"
        assert iterator.send("three") == "three"
        with pytest.raises(StopIteration) as stopped:
            next(iterator)
        assert stopped.value.value == "return-is-not-stream-text"
        assert [turn["final_message"] for turn in turns] == ["onetwothree"]
        with pytest.raises(StopIteration):
            next(iterator)
        assert len(turns) == 1
        assert cleanup == ["agent-alpha"]
        assert norma.current_agent() is None
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_stream_hold_raises_existing_error_with_aggregate_after_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma.finish import FinishHeldError

    turns = _mock_finish_client(monkeypatch, verdict="hold")
    app = SimpleNamespace()
    callbacks: list[dict[str, object]] = []

    def stream() -> Any:
        yield "draft "
        return "native-return"

    app.stream = stream
    app.on_verdict = callbacks.append
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {},
                "on_hold": "raise",
                "on_verdict": {"binding": "app.on_verdict"},
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert next(iterator) == "draft "
        with pytest.raises(FinishHeldError) as held:
            next(iterator)
        assert held.value.original_result == "draft "
        assert callbacks[0]["result"] == "draft "
        assert callbacks[0]["verdict"].verdict == "hold"
        assert turns[0]["final_message"] == "draft "
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_stream_hold_return_reports_callback_and_preserves_sync_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch, verdict="hold")
    app = SimpleNamespace()
    callbacks: list[dict[str, object]] = []

    def stream() -> Any:
        yield "final"
        return "sync-return"

    app.stream = stream
    app.on_verdict = callbacks.append
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {},
                "on_hold": "return",
                "on_verdict": {"binding": "app.on_verdict"},
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert next(iterator) == "final"
        with pytest.raises(StopIteration) as stopped:
            next(iterator)
        assert stopped.value.value == "sync-return"
        assert callbacks[0]["result"] == "final"
        assert callbacks[0]["verdict"].verdict == "hold"
        assert turns[0]["final_message"] == "final"
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_stream_last_mode_skips_missing_structured_text_and_counts_unicode_codepoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    chunks = [
        {"text": "a"},
        {"other": "missing path"},
        {"text": None},
        {"text": "😀"},
    ]

    async def stream() -> Any:
        for chunk in chunks:
            yield chunk

    app.stream = stream
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {
                    "text": {"path": ["result", "text"]},
                    "mode": "last",
                    "max_chars": 1,
                    "missing": "skip",
                },
                "on_hold": "raise",
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert [chunk async for chunk in iterator] == chunks
        assert turns[0]["final_message"] == "😀"
        assert len(turns) == 1
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_stream_overflow_closes_source_without_sending_partial_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma.checkpoints import CheckError

    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    cleanup: list[str | None] = []

    def stream() -> Any:
        try:
            yield "ab"
            yield "c"
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    configured = await norma.aconfigure(
        _stream_config(
            {"stream": {"max_chars": 2}, "on_hold": "raise"}
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert next(iterator) == "ab"
        with pytest.raises(CheckError, match="exceeds max_chars"):
            next(iterator)
        assert cleanup == ["agent-alpha"]
        assert turns == []
        assert norma.current_agent() is None
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_invalid_initial_sync_send_does_not_poison_stream_or_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()

    def stream() -> Any:
        yield "first"
        yield "second"

    app.stream = stream
    configured = await norma.aconfigure(
        _stream_config({"stream": {}, "on_hold": "raise"}),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        with pytest.raises(TypeError):
            iterator.send("before-start")
        assert next(iterator) == "first"
        assert next(iterator) == "second"
        iterator.close()
        assert turns == []
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_handled_throw_termination_finishes_sync_and_async_streams_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()

    def sync_stream() -> Any:
        try:
            yield "sync"
        except LookupError:
            return "sync-return"

    async def async_stream() -> Any:
        try:
            yield "async"
        except LookupError:
            return

    app.sync_stream = sync_stream
    app.async_stream = async_stream
    sync_config = _stream_config({"stream": {}, "on_hold": "raise"})
    sync_config["entrypoints"][0]["target"] = "app.sync_stream"
    async_config = _stream_config({"stream": {}, "on_hold": "raise"})
    async_config["entrypoints"][0]["target"] = "app.async_stream"
    config = {
        "version": 2,
        "entrypoints": sync_config["entrypoints"] + async_config["entrypoints"],
        "checkpoints": {},
    }
    configured = await norma.aconfigure(
        config,
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        sync_iterator = app.sync_stream()
        assert next(sync_iterator) == "sync"
        with pytest.raises(StopIteration) as stopped:
            sync_iterator.throw(LookupError("handled"))
        assert stopped.value.value == "sync-return"

        async_iterator = app.async_stream()
        assert await async_iterator.__anext__() == "async"
        with pytest.raises(StopAsyncIteration):
            await async_iterator.athrow(LookupError("handled"))
        assert sorted(turn["final_message"] for turn in turns) == ["async", "sync"]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_concurrent_async_advances_during_selector_fail_fast_without_reordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def select(invocation: dict[str, object]) -> str:
        entered.set()
        await release.wait()
        return str(invocation["result"])

    async def stream() -> Any:
        yield "first"
        yield "second"

    app.stream = stream
    app.select = select
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {"text": {"provider": "app.select"}},
                "on_hold": "raise",
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()

        first = asyncio.create_task(iterator.__anext__())
        await entered.wait()
        with pytest.raises(RuntimeError, match="already running"):
            await iterator.__anext__()
        assert turns == []
        release.set()
        assert await first == "first"
        assert await iterator.__anext__() == "second"
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()
        assert turns[0]["final_message"] == "firstsecond"
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_empty_stream_checks_empty_text_once_and_repeated_exhaustion_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()

    def stream() -> Any:
        if False:
            yield "unreachable"

    app.stream = stream
    configured = await norma.aconfigure(
        _stream_config({"stream": {}, "on_hold": "raise"}),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        with pytest.raises(StopIteration):
            next(iterator)
        with pytest.raises(StopIteration):
            next(iterator)
        assert [turn["final_message"] for turn in turns] == [""]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_stream_body_and_selector_are_lazy_until_the_first_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    calls: list[str] = []

    async def stream() -> Any:
        calls.append("body")
        yield "chunk"

    def select(invocation: dict[str, object]) -> str:
        calls.append("selector")
        return str(invocation["result"])

    app.stream = stream
    app.select = select
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {"text": {"provider": "app.select"}},
                "on_hold": "raise",
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        assert calls == []
        iterator = app.stream()
        assert calls == []
        assert await iterator.__anext__() == "chunk"
        assert calls == ["body", "selector"]
        await iterator.aclose()
        assert turns == []
    finally:
        await configured.aclose()


@pytest.mark.parametrize(
    ("chunk", "missing", "expected"),
    [
        ({"other": "no text"}, "error", "path is unavailable"),
        ({"text": 4}, "skip", "must resolve to a string"),
    ],
)
@pytest.mark.asyncio
async def test_stream_missing_error_and_nonstring_skip_are_collection_failures(
    chunk: dict[str, object],
    missing: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma.checkpoints import CheckError

    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    cleanup: list[str | None] = []

    def stream() -> Any:
        try:
            yield chunk
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {
                    "text": {"path": ["result", "text"]},
                    "missing": missing,
                },
                "on_hold": "raise",
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        with pytest.raises(CheckError, match=expected):
            next(iterator)
        assert cleanup == ["agent-alpha"]
        assert turns == []
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_provider_failure_is_not_suppressed_by_missing_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma.checkpoints import CheckError

    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    cleanup: list[str | None] = []

    def select(_invocation: dict[str, object]) -> None:
        raise KeyError("provider failed")

    def stream() -> Any:
        try:
            yield {"text": "present"}
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    app.select = select
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {"text": {"provider": "app.select"}, "missing": "skip"},
                "on_hold": "raise",
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        with pytest.raises(CheckError, match="selector failed"):
            next(app.stream())
        assert turns == []
        assert cleanup == ["agent-alpha"]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_configured_stream_early_close_cancellation_and_body_error_never_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    cleanup: list[str | None] = []
    entered = asyncio.Event()

    async def stream() -> Any:
        try:
            yield "first"
            entered.set()
            await asyncio.Event().wait()
            yield "never"
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    async def failing_stream() -> Any:
        try:
            yield "first"
            raise RuntimeError("stream body failed")
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    app.failing_stream = failing_stream
    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [
                {
                    **_stream_config({"stream": {}, "on_hold": "raise"})["entrypoints"][0],
                    "target": "app.stream",
                },
                {
                    **_stream_config({"stream": {}, "on_hold": "raise"})["entrypoints"][0],
                    "target": "app.failing_stream",
                },
            ],
            "checkpoints": {},
        },
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        early = app.stream()
        assert await early.__anext__() == "first"
        await early.aclose()

        cancelled = app.stream()
        assert await cancelled.__anext__() == "first"
        task = asyncio.create_task(cancelled.__anext__())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        failing = app.failing_stream()
        assert await failing.__anext__() == "first"
        with pytest.raises(RuntimeError, match="stream body failed"):
            await failing.__anext__()
        assert turns == []
        assert cleanup == ["agent-alpha", "agent-alpha", "agent-alpha"]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_cancellation_handled_as_async_generator_return_does_not_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    entered = asyncio.Event()
    cleanup: list[str | None] = []

    async def stream() -> Any:
        try:
            yield "partial"
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    configured = await norma.aconfigure(
        _stream_config({"stream": {}, "on_hold": "raise"}),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert await iterator.__anext__() == "partial"
        task = asyncio.create_task(iterator.__anext__())
        await entered.wait()
        task.cancel()
        with pytest.raises(StopAsyncIteration):
            await task
        assert turns == []
        assert cleanup == ["agent-alpha"]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_cancellation_handled_as_yield_keeps_stream_finish_aborted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    entered = asyncio.Event()
    cleanup: list[str | None] = []

    async def stream() -> Any:
        try:
            yield "before-cancel"
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                yield "after-cancel"
            yield "later"
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    configured = await norma.aconfigure(
        _stream_config({"stream": {}, "on_hold": "raise"}),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert await iterator.__anext__() == "before-cancel"
        cancelled_advance = asyncio.create_task(iterator.__anext__())
        await entered.wait()
        cancelled_advance.cancel()
        assert await cancelled_advance == "after-cancel"

        # A different consumer task resumes and naturally exhausts the source.
        assert await asyncio.create_task(iterator.__anext__()) == "later"
        with pytest.raises(StopAsyncIteration):
            await asyncio.create_task(iterator.__anext__())
        assert turns == []
        assert cleanup == ["agent-alpha"]
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_close_during_pending_selector_blocks_finish_and_cleans_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma.checkpoints import CheckError

    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    selector_started = asyncio.Event()
    release_selector = asyncio.Event()
    cleanup: list[str | None] = []

    async def select(invocation: dict[str, object]) -> str:
        selector_started.set()
        await release_selector.wait()
        return str(invocation["result"])

    async def stream() -> Any:
        try:
            yield "chunk"
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    app.select = select
    app.on_verdict = lambda _payload: pytest.fail("finish callback ran after close")
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {"text": {"provider": "app.select"}},
                "on_hold": "raise",
                "on_verdict": {"binding": "app.on_verdict"},
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        task = asyncio.create_task(iterator.__anext__())
        await selector_started.wait()
        await configured.aclose()
        release_selector.set()
        with pytest.raises(CheckError, match="closed"):
            await task
        assert turns == []
        assert cleanup == ["agent-alpha"]
    finally:
        release_selector.set()
        await configured.aclose()


@pytest.mark.asyncio
async def test_close_during_pending_advance_blocks_chunk_and_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma.checkpoints import CheckError

    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    advance_started = asyncio.Event()
    release_advance = asyncio.Event()
    cleanup: list[str | None] = []

    async def stream() -> Any:
        try:
            advance_started.set()
            await release_advance.wait()
            yield "chunk"
        finally:
            agent = norma.current_agent()
            cleanup.append(agent.agent_name if agent is not None else None)

    app.stream = stream
    configured = await norma.aconfigure(
        _stream_config({"stream": {}, "on_hold": "raise"}),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        task = asyncio.create_task(iterator.__anext__())
        await advance_started.wait()
        await configured.aclose()
        release_advance.set()
        with pytest.raises(CheckError, match="closed"):
            await task
        assert turns == []
        assert cleanup == ["agent-alpha"]
    finally:
        release_advance.set()
        await configured.aclose()


@pytest.mark.asyncio
async def test_close_during_finish_check_prevents_verdict_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma.checkpoints import CheckError

    _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    check_started = asyncio.Event()
    release_check = asyncio.Event()
    callbacks: list[dict[str, object]] = []

    async def pending_check(_turn: object) -> None:
        check_started.set()
        await release_check.wait()

    async def stream() -> Any:
        yield "complete"

    app.stream = stream
    app.on_verdict = callbacks.append
    monkeypatch.setattr("norma.finish.check_finish", pending_check)
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {},
                "on_hold": "raise",
                "on_verdict": {"binding": "app.on_verdict"},
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert await iterator.__anext__() == "complete"
        task = asyncio.create_task(iterator.__anext__())
        await check_started.wait()
        await configured.aclose()
        release_check.set()
        with pytest.raises(CheckError, match="closed"):
            await task
        assert callbacks == []
    finally:
        release_check.set()
        await configured.aclose()


@pytest.mark.asyncio
async def test_close_during_verdict_callback_stops_stream_before_exhaustion_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from norma.checkpoints import CheckError

    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callbacks: list[dict[str, object]] = []

    async def on_verdict(payload: dict[str, object]) -> None:
        callbacks.append(payload)
        callback_started.set()
        await release_callback.wait()

    async def stream() -> Any:
        yield "complete"

    app.stream = stream
    app.on_verdict = on_verdict
    configured = await norma.aconfigure(
        _stream_config(
            {
                "stream": {},
                "on_hold": "raise",
                "on_verdict": {"binding": "app.on_verdict"},
            }
        ),
        agent_name="agent-alpha",
        bindings={"app": app},
        connection={"runtime_url": URL, "agent_key": "key-alpha", "observability_enabled": False},
    )
    try:
        iterator = app.stream()
        assert await iterator.__anext__() == "complete"
        task = asyncio.create_task(iterator.__anext__())
        await callback_started.wait()
        await configured.aclose()
        release_callback.set()
        with pytest.raises(CheckError, match="closed"):
            await task
        assert len(turns) == 1
        assert len(callbacks) == 1
    finally:
        release_callback.set()
        await configured.aclose()


@pytest.mark.asyncio
async def test_two_agent_stream_finish_interleavings_keep_scopes_and_records_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _mock_finish_client(monkeypatch)
    app = SimpleNamespace()
    release = asyncio.Event()
    callbacks: list[tuple[str, str, str]] = []

    async def alpha_stream() -> Any:
        yield "A"
        await release.wait()
        yield "a"

    async def bravo_stream() -> Any:
        yield "B"
        await release.wait()
        yield "b"

    def on_verdict(payload: dict[str, object]) -> None:
        agent = norma.current_agent()
        assert agent is not None
        callbacks.append((agent.agent_name, str(payload["result"]), str(payload["agent_name"])))

    app.alpha_stream = alpha_stream
    app.bravo_stream = bravo_stream
    app.on_verdict = on_verdict
    alpha_config = _stream_config(
        {
            "stream": {},
            "on_hold": "raise",
            "on_verdict": {"binding": "app.on_verdict"},
        }
    )
    alpha_config["entrypoints"][0]["target"] = "app.alpha_stream"
    bravo_config = _stream_config(
        {
            "stream": {},
            "on_hold": "raise",
            "on_verdict": {"binding": "app.on_verdict"},
        }
    )
    bravo_config["entrypoints"][0]["target"] = "app.bravo_stream"
    config_dir = tempfile.TemporaryDirectory()
    directory = Path(config_dir.name)
    (directory / "agent-alpha.json").write_text(json.dumps(alpha_config), encoding="utf-8")
    (directory / "agent-bravo.json").write_text(json.dumps(bravo_config), encoding="utf-8")
    configured = await norma.aconfigure(
        directory,
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
    try:
        alpha = app.alpha_stream()
        bravo = app.bravo_stream()
        assert await alpha.__anext__() == "A"
        assert norma.current_agent() is None
        assert await bravo.__anext__() == "B"
        assert norma.current_agent() is None
        release.set()
        assert await alpha.__anext__() == "a"
        assert norma.current_agent() is None
        assert await bravo.__anext__() == "b"
        assert norma.current_agent() is None

        async def exhaust(iterator: Any) -> None:
            with pytest.raises(StopAsyncIteration):
                await iterator.__anext__()

        await asyncio.gather(exhaust(alpha), exhaust(bravo))
        assert sorted((turn["agent_name"], turn["final_message"]) for turn in turns) == [
            ("agent-alpha", "Aa"),
            ("agent-bravo", "Bb"),
        ]
        assert sorted(callbacks) == [
            ("agent-alpha", "Aa", "agent-alpha"),
            ("agent-bravo", "Bb", "agent-bravo"),
        ]
        assert norma.current_agent() is None
    finally:
        await configured.aclose()
        config_dir.cleanup()
