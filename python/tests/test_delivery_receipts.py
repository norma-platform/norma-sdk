# Modified for Norma.
"""Offline delivery receipts: HTTP acceptance is not proof of stored records."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from norma.models import RuleCheckEvent
from norma.observability import EventBatcher
from norma.settings import configure_settings, get_settings


@pytest.fixture(autouse=True)
def delivery_settings():
    # Given: bounded deterministic retries and no real transports.
    original = get_settings().model_dump()
    configure_settings(max_retries=1, retry_delay=0.001, observability_enabled=True)
    yield
    configure_settings(**original)


def event() -> RuleCheckEvent:
    return RuleCheckEvent(
        check_id="8be95526-a6bc-4e63-9dc6-1426f202944a",
        trace_id="a" * 32,
        span_id="b" * 16,
        agent_name="receipt-agent",
        rule_id=1,
        rule_name="price-rule",
        check_stage="post",
        applies_to="tool",
        action="observe",
        matched=False,
        confidence=1,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


def receipt_body(**overrides):
    return {
        "received": 1,
        "enqueued": 1,
        "dropped": 0,
        "stored": 1,
        "duplicates": 0,
        "status": "queued",
        **overrides,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize(
    ("body", "status", "persisted", "pending"),
    [
        (receipt_body(), "delivered", 1, 0),
        (receipt_body(stored=0, duplicates=1), "delivered", 1, 0),
        (receipt_body(stored=None), "acknowledged", 0, 0),
        (receipt_body(enqueued=0, stored=0, dropped=1, status="failed"), "pending", 0, 1),
        (receipt_body(status="partial"), "pending", 0, 1),
        (receipt_body(received=2), "pending", 0, 1),
        (receipt_body(enqueued=True), "pending", 0, 1),
        ({"status": "queued"}, "pending", 0, 1),
    ],
)
async def test_complete_body_required_for_delivery(sync, body, status, persisted, pending):
    # Given: one queued event and an in-memory ingestion acknowledgement.
    batcher = EventBatcher(runtime_url="http://norma.test")
    batcher.add_event(event())
    transport = httpx.MockTransport(lambda request: httpx.Response(202, json=body))

    # When: flushing through the actual sync or async HTTP path.
    if sync:
        client = httpx.Client(transport=transport)
        with patch("norma.observability.httpx.Client", return_value=client):
            batcher._flush_sync()  # Exercise shutdown's otherwise private transport path.
        result = batcher.get_delivery_receipt()
    else:
        batcher._client = httpx.AsyncClient(transport=transport)
        result = await batcher.flush_all()

    # Then: only confirmed persistence permits a delivered result.
    assert result.status == status
    assert result.persisted == persisted
    assert result.pending == pending
    assert result.acknowledged == (0 if pending else 1)
    assert result.dropped == 0
    assert result.message


@pytest.mark.asyncio
async def test_partial_batch_retry_confirms_all_original_event_ids():
    # Given: partial ingestion without per-event IDs, followed by an idempotent replay.
    batcher = EventBatcher(runtime_url="http://norma.test")
    first = event()
    second = first.model_copy(update={"check_id": "different-execution"})
    batcher.add_event(first)
    batcher.add_event(second)
    requests: list[bytes] = []
    responses = [
        receipt_body(received=2, enqueued=1, dropped=1, stored=1, status="partial"),
        receipt_body(received=2, enqueued=2, stored=1, duplicates=1),
    ]

    def ingest(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return httpx.Response(202, json=responses.pop(0))

    batcher._client = httpx.AsyncClient(transport=httpx.MockTransport(ingest))

    # When: the user flushes twice, retrying the uncertain batch unchanged.
    partial = await batcher.flush_all(close_client=False)
    complete = await batcher.flush_all()

    # Then: no partial success is inflated, and the final receipt covers both rows.
    assert partial.status == "pending"
    assert partial.pending == 2
    assert partial.acknowledged == 0
    assert requests[0] == requests[1]
    assert complete.status == "delivered"
    assert complete.acknowledged == complete.persisted == 2
    assert complete.pending == 0


@pytest.mark.asyncio
async def test_network_failure_does_not_claim_delivery_or_expose_exception(caplog):
    # Given: an offline transport failure containing a sentinel sensitive URL.
    batcher = EventBatcher(runtime_url="http://norma.test")
    batcher.add_event(event())

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("https://sensitive.invalid/private-token")

    batcher._client = httpx.AsyncClient(transport=httpx.MockTransport(fail))

    # When: flushing cannot connect.
    result = await batcher.flush_all()

    # Then: the event remains retryable, with safe actionable guidance.
    assert result.status == "pending"
    assert result.acknowledged == result.persisted == 0
    assert result.pending == 1
    assert "server URL" in result.message
    assert "private-token" not in caplog.text


def test_shutdown_returns_terminal_failure_for_unconfirmed_events():
    # Given: the real fallback path receives a failed202 acknowledgement.
    batcher = EventBatcher(runtime_url="http://norma.test")
    batcher.add_event(event())
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            202,
            json=receipt_body(enqueued=0, dropped=1, stored=0, status="failed"),
        )
    )

    # When: shutdown exhausts its bounded attempts.
    client = httpx.Client(transport=transport)
    with patch("norma.observability.httpx.Client", return_value=client):
        result = batcher.shutdown()

    # Then: exiting does not silently claim delivery.
    assert result.status == "failed"
    assert result.acknowledged == result.persisted == result.pending == 0
    assert result.dropped == 1
    assert "server storage" in result.message


@pytest.mark.asyncio
async def test_in_flight_flush_is_not_mistaken_for_empty_delivery():
    # Given: an event remains in an unfinished transport request.
    batcher = EventBatcher(runtime_url="http://norma.test")
    batcher.add_event(event())
    started = asyncio.Event()
    release = asyncio.Event()

    async def ingest(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(202, json=receipt_body())

    batcher._client = httpx.AsyncClient(transport=httpx.MockTransport(ingest))
    task = asyncio.create_task(batcher.flush_all())
    await started.wait()

    # When: reading the receipt during delivery.
    pending = batcher.get_delivery_receipt()
    release.set()
    delivered = await task

    # Then: pending includes in-flight rows, and confirmation follows the body.
    assert pending.status == "pending"
    assert pending.pending == 1
    assert delivered.status == "delivered"


@pytest.mark.parametrize("async_api", [False, True])
def test_public_flush_returns_confirmed_receipt(monkeypatch, async_api):
    # Given: a real batcher with in-memory ingestion and no worker thread.
    import norma
    import norma.observability as obs

    batcher = EventBatcher(runtime_url="http://norma.test")
    batcher.add_event(event())
    monkeypatch.setattr(obs, "_batcher", batcher)
    monkeypatch.setattr(obs, "_event_sink", obs._BatcherRuleEventSink(batcher))
    transport = httpx.MockTransport(lambda request: httpx.Response(202, json=receipt_body()))
    client = httpx.Client(transport=transport)

    # When: using the exported flush facade.
    with patch("norma.observability.httpx.Client", return_value=client):
        result = asyncio.run(norma.aflush()) if async_api else norma.flush()

    # Then: callers receive storage evidence, and the batcher stays usable.
    assert isinstance(result, norma.DeliveryReceipt)
    assert result.status == "delivered"
    assert result.persisted == result.acknowledged == 1
    assert obs.get_event_batcher() is batcher


def test_custom_sink_flush_failure_is_reported_without_affecting_enforcement(monkeypatch):
    # Given: a custom sink explicitly reports an exporter flush failure.
    import norma.observability as obs

    configure_settings(observability_sink_name="registered")
    sink = MagicMock()
    sink.flush.return_value = False
    monkeypatch.setattr(obs, "_batcher", None)
    monkeypatch.setattr(obs, "_get_active_rule_event_sinks", lambda: (sink,))
    monkeypatch.setattr(obs, "_custom_events_dropped", 0)

    # When: flushing telemetry independently of check.
    result = obs.sync_flush_observability()

    # Then: an exporter failure cannot masquerade as a hosted record.
    assert result.status == "failed"
    assert result.persisted == 0
    assert "exporter configuration" in result.message


def test_disabled_recording_is_explicit(monkeypatch):
    # Given: the caller intentionally disables observability.
    import norma.observability as obs

    configure_settings(observability_enabled=False)
    monkeypatch.setattr(obs, "_batcher", None)

    # When: checking delivery.
    result = obs.sync_flush_observability()

    # Then: disabled recording is distinct from an empty or successful batch.
    assert result.status == "disabled"
    assert result.persisted == result.acknowledged == 0


def test_reinitialize_rotates_batcher_without_reusing_previous_credentials(monkeypatch):
    # Given: an existing session and an authorized new resolved connection.
    import norma.observability as obs

    configure_settings(observability_sink_name="default")
    first = EventBatcher(runtime_url="http://first.test", agent_key="fixture-first")
    monkeypatch.setattr(obs, "_batcher", first)
    monkeypatch.setattr(obs, "_event_sink", obs._BatcherRuleEventSink(first))

    # When: reinitializing; the thread and exit hook are stubbed to keep this offline.
    with patch.object(EventBatcher, "start"), patch("norma.observability.atexit.register"):
        second = obs.init_observability(
            runtime_url="http://second.test/",
            agent_key="fixture-second",
        )

    # Then: fresh events use the new resolved connection; the old one is retained only locally.
    assert second is not None and second is not first
    assert second.runtime_url == "http://second.test"
    assert second.agent_key == "fixture-second"
    assert first.agent_key == "fixture-first"


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_acknowledged", [False, True])
async def test_shutdown_marks_abandoned_inflight_delivery_terminal(
    monkeypatch,
    previous_acknowledged,
):
    # Given: a suspended transport, optionally following an acknowledgement without storage proof.
    batcher = EventBatcher(runtime_url="http://norma.test")
    if previous_acknowledged:
        batcher.add_event(event())
        batcher._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(202, json=receipt_body(stored=None)),
            )
        )
        assert (await batcher.flush_all()).status == "acknowledged"
    batcher.add_event(event().model_copy(update={"check_id": "late-execution"}))
    started = asyncio.Event()
    release = asyncio.Event()

    async def ingest(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(202, json=receipt_body())

    batcher._client = httpx.AsyncClient(transport=httpx.MockTransport(ingest))
    task = asyncio.create_task(batcher.flush_all())
    await started.wait()
    monkeypatch.setattr(batcher, "_stop_worker", lambda **kwargs: False)

    # When: shutdown abandons the in-flight batch, then its callback arrives late.
    terminal = batcher.shutdown()
    release.set()
    late = await task

    # Then: the cleared lifecycle never claims a retryable pending batch or delivery.
    assert terminal.status == late.status == "failed"
    assert terminal.pending == late.pending == 0
    assert terminal.dropped == late.dropped == 1
    assert late.acknowledged == int(previous_acknowledged)
    assert late.persisted == 0
    assert batcher.add_event(event()) is False


def test_emergency_worker_stop_cancels_and_accounts_for_inflight_batch():
    # Given: internal send suspension forces the otherwise rare emergency stop path.
    import threading

    batcher = EventBatcher(runtime_url="http://norma.test", batch_size=1)
    batcher.shutdown_join_timeout = 0
    started = threading.Event()

    async def blocked(events):
        started.set()
        await asyncio.Event().wait()
        return True

    with patch.object(batcher, "_send_batch", side_effect=blocked):
        batcher.start()
        batcher.add_event(event())
        assert started.wait(timeout=1)

        # When: cancellation requeues the batch but the bounded fallback also fails.
        with patch.object(batcher, "_send_batch_sync", return_value=False):
            result = batcher.shutdown()

    # Then: the thread stopped, and all unconfirmed work is accounted for as terminal.
    assert result.status == "failed"
    assert result.pending == 0
    assert result.dropped == 1
    assert batcher._thread is None


def test_no_active_custom_sink_retains_known_drop_in_receipt(monkeypatch):
    # Given: a selected but unavailable exporter.
    import norma.observability as obs

    configure_settings(observability_sink_name="otel")
    monkeypatch.setattr(obs, "_batcher", None)
    monkeypatch.setattr(obs, "_configured_named_event_sink", None)
    monkeypatch.setattr(obs, "_get_active_rule_event_sinks", lambda: ())
    monkeypatch.setattr(obs, "_custom_events_dropped", 0)

    # When: a caller attempts to record an event and then shuts down.
    queued = obs.write_events([event()])
    result = obs.sync_shutdown_observability()

    # Then: a known drop does not become merely unconfirmed with zero losses.
    assert queued.dropped == result.dropped == 1
    assert result.status == "failed"


def test_new_session_cannot_inherit_previous_sessions_delivered_receipt(monkeypatch):
    # Given: one previous session has confirmed stored a record.
    import norma.observability as obs

    configure_settings(observability_sink_name="default")
    first = EventBatcher(runtime_url="http://norma.test", agent_key="fixture-shared")
    first.add_event(event())
    first._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(202, json=receipt_body()),
        )
    )
    assert asyncio.run(first.flush_all()).status == "delivered"
    monkeypatch.setattr(obs, "_batcher", first)
    monkeypatch.setattr(obs, "_event_sink", obs._BatcherRuleEventSink(first))

    # When: initialization succeeds for a new session using the same connection.
    with patch.object(EventBatcher, "start"), patch("norma.observability.atexit.register"):
        fresh = obs.init_observability(
            runtime_url="http://norma.test",
            agent_key="fixture-shared",
            new_session=True,
        )

    # Then: the new session has no Rule records until it runs matching checkpoints.
    assert fresh is not None and fresh is not first
    assert fresh.get_delivery_receipt().status == "empty"
    assert fresh.get_delivery_receipt().persisted == 0
