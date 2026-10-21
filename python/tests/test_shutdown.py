# Modified for Norma.
"""Tests for norma.shutdown() and norma.ashutdown()."""

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import norma
import norma.observability as obs_mod
from norma._state import state
from norma.observability import EventBatcher


def _make_started_batcher() -> EventBatcher:
    """Create a batcher with a mocked _send_batch and start it."""
    batcher = EventBatcher(batch_size=100, flush_interval=60.0)
    batcher._send_batch = AsyncMock(return_value=True)
    batcher.start()
    return batcher


class TestShutdownSync:
    """Tests for the synchronous shutdown() function."""

    def test_shutdown_flushes_batcher(self):
        batcher = _make_started_batcher()
        obs_mod._batcher = batcher

        mock_event = MagicMock()
        mock_event.model_dump = MagicMock(return_value={"test": True})
        for _ in range(3):
            batcher.add_event(mock_event)

        norma.shutdown()

        assert batcher._events_sent == 3
        assert len(batcher._events) == 0
        assert obs_mod._batcher is None

    def test_shutdown_stops_policy_refresh(self):
        stop_event = threading.Event()
        thread = threading.Thread(target=stop_event.wait, daemon=True)
        thread.start()

        norma._refresh_thread = thread
        norma._refresh_stop_event = stop_event
        norma._policy_refresh_interval_seconds = 60

        norma.shutdown()

        assert stop_event.is_set()
        assert norma._refresh_thread is None
        assert norma._refresh_stop_event is None

    def test_shutdown_resets_state(self):
        state.current_agent = MagicMock()
        state.rule_engine = MagicMock()
        state.server_rules = [{"name": "test"}]
        state.server_url = "http://localhost:8000"
        state.api_key = "key"
        state.api_key_header = "X-Custom-API-Key"

        norma.shutdown()

        assert state.current_agent is None
        assert state.rule_engine is None
        assert state.server_rules is None
        assert state.server_url is None
        assert state.api_key is None
        assert state.api_key_header is None

    def test_shutdown_idempotent(self):
        norma.shutdown()
        norma.shutdown()

    def test_shutdown_without_init(self):
        """shutdown() should be safe to call even if init() was never called."""
        state.current_agent = None
        obs_mod._batcher = None
        norma._refresh_thread = None

        norma.shutdown()


class TestAshutdownAsync:
    """Tests for the async ashutdown() function."""

    @pytest.mark.asyncio
    async def test_ashutdown_flushes_batcher(self):
        batcher = _make_started_batcher()
        obs_mod._batcher = batcher

        mock_event = MagicMock()
        mock_event.model_dump = MagicMock(return_value={"test": True})
        for _ in range(3):
            batcher.add_event(mock_event)

        await norma.ashutdown()

        assert batcher._events_sent == 3
        assert obs_mod._batcher is None

    @pytest.mark.asyncio
    async def test_ashutdown_resets_state(self):
        state.current_agent = MagicMock()
        state.server_url = "http://localhost:8000"

        await norma.ashutdown()

        assert state.current_agent is None
        assert state.server_url is None

    @pytest.mark.asyncio
    async def test_ashutdown_idempotent(self):
        await norma.ashutdown()
        await norma.ashutdown()

    @pytest.mark.asyncio
    async def test_ashutdown_stops_refresh_loops_off_thread(self):
        with (
            patch(
                "norma.asyncio.to_thread",
                new=AsyncMock(return_value=None),
            ) as to_thread_mock,
            patch(
                "norma.shutdown_observability",
                new=AsyncMock(return_value=None),
            ) as shutdown_observability_mock,
        ):
            await norma.ashutdown()

        awaited_targets = [call.args[0] for call in to_thread_mock.await_args_list]
        assert awaited_targets == [norma._stop_policy_refresh_loop]
        shutdown_observability_mock.assert_awaited_once()


class TestShutdownRefreshRace:
    """Regression tests: in-flight refresh must not publish after shutdown.

    These tests run _policy_refresh_worker directly (like the existing
    worker tests) with a gated _fetch_rules_async, so the actual
    post-fetch stop_event check in the worker is what prevents the
    zombie publish.
    """

    def test_sync_shutdown_prevents_zombie_publish(self):
        fetch_gate = threading.Event()
        fetch_entered = threading.Event()
        stop_event = threading.Event()

        async def slow_fetch() -> list[dict[str, Any]]:
            fetch_entered.set()
            await asyncio.to_thread(fetch_gate.wait)
            return [{"name": "zombie-rule", "rule": {}}]

        state.current_agent = None
        state.server_rules = None

        # Wire up module state so shutdown() can signal the stop event.
        norma._refresh_stop_event = stop_event

        with patch(
            "norma._fetch_rules_async",
            new=AsyncMock(side_effect=slow_fetch),
        ):
            # Run the real worker in a thread (interval=1 but the first
            # wait(1) returns False immediately since stop isn't set).
            worker_thread = threading.Thread(
                target=norma._policy_refresh_worker,
                args=(stop_event, 0),
                daemon=True,
            )
            norma._refresh_thread = worker_thread
            worker_thread.start()

            assert fetch_entered.wait(timeout=5), "worker never entered fetch"

            # Shutdown while fetch is in-flight.
            norma.shutdown()

            # Unblock the fetch - worker should see stop_event and discard.
            fetch_gate.set()
            worker_thread.join(timeout=5)

        assert state.server_rules is None

    @pytest.mark.asyncio
    async def test_async_shutdown_prevents_zombie_publish(self):
        fetch_gate = threading.Event()
        fetch_entered = threading.Event()
        stop_event = threading.Event()

        async def slow_fetch() -> list[dict[str, Any]]:
            fetch_entered.set()
            await asyncio.to_thread(fetch_gate.wait)
            return [{"name": "zombie-rule", "rule": {}}]

        state.current_agent = None
        state.server_rules = None

        norma._refresh_stop_event = stop_event

        with patch(
            "norma._fetch_rules_async",
            new=AsyncMock(side_effect=slow_fetch),
        ):
            worker_thread = threading.Thread(
                target=norma._policy_refresh_worker,
                args=(stop_event, 0),
                daemon=True,
            )
            norma._refresh_thread = worker_thread
            worker_thread.start()

            assert fetch_entered.wait(timeout=5), "worker never entered fetch"

            await norma.ashutdown()

            fetch_gate.set()
            worker_thread.join(timeout=5)

        assert state.server_rules is None


class TestManualRefreshLifecycleRace:
    """Regression tests for public refresh calls racing with lifecycle changes."""

    @pytest.mark.asyncio
    async def test_manual_refresh_discards_result_after_shutdown(self):
        fetch_gate = threading.Event()
        fetch_entered = threading.Event()

        async def slow_list_agent_rules(*args: Any, **kwargs: Any) -> dict[str, Any]:
            fetch_entered.set()
            await asyncio.to_thread(fetch_gate.wait)
            return {"rules": []}

        with (
            patch(
                "norma.__init__.NormaClient.check_connection",
                new=AsyncMock(return_value={"status": "healthy"}),
            ),
            patch(
                "norma.__init__.agents._register_agent",
                new=AsyncMock(return_value={"created": True, "rules": []}),
            ),
            patch(
                "norma.__init__.agents._list_agent_rules",
                new=AsyncMock(side_effect=slow_list_agent_rules),
            ),
        ):
            norma.init(
                agent_name="manual-refresh-shutdown-agent",
                policy_refresh_interval_seconds=0,
            )
            refresh_task = asyncio.create_task(norma.refresh_rules_async())

            assert await asyncio.to_thread(fetch_entered.wait, 5), (
                "manual refresh never entered fetch"
            )

            norma.shutdown()
            fetch_gate.set()
            refreshed_snapshot = await refresh_task

        assert refreshed_snapshot is None
        assert state.server_rules is None
