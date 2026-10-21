# Modified for Norma.
"""
Event batching and transmission for Norma observability.

This module provides:
1. EventBatcher for collecting and sending rule check events to the server
2. Standard library-compliant logging setup for the SDK

Logging:
    The SDK follows Python logging best practices:
    - Uses hierarchical logger names (norma.*)
    - Adds only NullHandler (no formatters or handlers)
    - Applications Rule log configuration via logging.basicConfig() or handlers

    Example - Configure logging in your application:
        import logging
        logging.basicConfig(level=logging.INFO)
        logging.getLogger('norma').setLevel(logging.DEBUG)

Event Batching Usage:
    from norma.observability import get_event_batcher, add_event

    # Add an event (usually done automatically by @checkpoint decorator)
    add_event(event)

    # On shutdown, flush remaining events
    await shutdown_observability()

Configuration (Environment Variables):
    # Server connection
    NORMA_RUNTIME_URL and NORMA_AGENT_KEY configure hosted delivery.

    # Observability (event batching)
    NORMA_OBSERVABILITY_ENABLED: Enable observability (default: true)
    NORMA_OBSERVABILITY_SINK_NAME: Selected rule-event sink (default: default)
    NORMA_OBSERVABILITY_SINK_CONFIG: JSON config for the selected sink
    NORMA_OTEL_ENABLED: Enable the built-in OTEL sink (default: false)
    NORMA_OTEL_ENDPOINT: OTLP HTTP endpoint for exported rule-event spans
    NORMA_OTEL_HEADERS: JSON object of OTLP exporter headers
    NORMA_OTEL_SERVICE_NAME: OTEL service.name for emitted spans
    NORMA_BATCH_SIZE: Max events per batch (default: 100)
    NORMA_FLUSH_INTERVAL: Seconds between flushes (default: 5.0)
    NORMA_SHUTDOWN_JOIN_TIMEOUT: Seconds to wait for worker shutdown (default: 5.0)
    NORMA_SHUTDOWN_FLUSH_TIMEOUT: Seconds to wait for fallback flush (default: 5.0)
    NORMA_SHUTDOWN_MAX_FAILED_FLUSHES: Consecutive failed flushes before stop (default: 1)

    # SDK Logging Behavior (what logs to emit)
    NORMA_LOG_ENABLED: Master switch for SDK logging (default: true)
    NORMA_LOG_SPAN_START: Emit span start logs (default: true)
    NORMA_LOG_SPAN_END: Emit span end logs (default: true)
    NORMA_LOG_RULE_EVAL: Emit per-rule check logs (default: true)
"""

from __future__ import annotations

import asyncio
import atexit
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import httpx

from norma._telemetry.sink_selection import (
    DEFAULT_RULE_EVENT_SINK_NAME,
    REGISTERED_RULE_EVENT_SINK_NAME,
    RuleEventSinkFactory,
    RuleEventSinkFactoryRegistry,
    RuleEventSinkSelection,
    SinkSelectionError,
)
from norma._telemetry.sinks import (
    BaseRuleEventSink,
    RuleEventSink,
    SinkResult,
)
from norma.models import JSONObject
from norma.models.observability import BatchEventsResponse
from norma.settings import configure_settings, get_settings

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

    from norma.models import RuleCheckEvent

from .otel_sink import (
    OTEL_RULE_EVENT_SINK_NAME,
    create_otel_rule_event_sink,
)

# =============================================================================
# Logger Setup - Standard Library Pattern
# =============================================================================
#
# Following Python logging best practices for libraries:
# - Use hierarchical logger names (norma.*)
# - Add only NullHandler to suppress "No handler" warnings
# - Let applications configure handlers, formatters, and levels
# - Provide behavioral settings to Rule what the SDK logs
#
# Applications should configure norma loggers like this:
#   import logging
#   logging.basicConfig(level=logging.INFO)
#   logging.getLogger('norma').setLevel(logging.DEBUG)

_ROOT_LOGGER_NAME = "norma"


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger for a specific module under the norma namespace.

    This follows standard library conventions - applications Rule logging
    configuration (handlers, formatters, levels), while the SDK just creates
    properly namespaced loggers.

    Args:
        name: Module name (typically __name__)

    Returns:
        Logger instance under norma namespace

    Example:
        logger = get_logger(__name__)
        logger.info("Processing started")
    """
    if not name.startswith(_ROOT_LOGGER_NAME):
        name = f"{_ROOT_LOGGER_NAME}.{name}"
    return logging.getLogger(name)


# Add NullHandler to root logger following standard library pattern
# This suppresses "No handlers could be found" warnings while allowing
# applications to configure logging as needed
logging.getLogger(_ROOT_LOGGER_NAME).addHandler(logging.NullHandler())

# Module logger
logger = get_logger(__name__)

# =============================================================================
# Logging configuration facade
# =============================================================================


@dataclass
class LogConfig:
    """
    Configuration for SDK logging behavior.

    Settings are stored in the centralized SDKSettings instance.

    Applications should configure log levels via Python's standard logging
    module (e.g., logging.getLogger('norma').setLevel(logging.DEBUG)).
    The fields in this class select which categories of logs the SDK emits.
    """

    enabled: bool = field(default_factory=lambda: get_settings().log_enabled)
    span_start: bool = field(default_factory=lambda: get_settings().log_span_start)
    span_end: bool = field(default_factory=lambda: get_settings().log_span_end)
    rule_eval: bool = field(default_factory=lambda: get_settings().log_rule_eval)

    @classmethod
    def from_env(cls) -> LogConfig:
        """Load configuration from environment variables (via settings)."""
        return cls(
            enabled=get_settings().log_enabled,
            span_start=get_settings().log_span_start,
            span_end=get_settings().log_span_end,
            rule_eval=get_settings().log_rule_eval,
        )

    def update(self, config_dict: dict[str, Any]) -> None:
        """Update configuration from a dictionary."""
        # Map old names to new setting names
        mapping = {
            "enabled": "log_enabled",
            "span_start": "log_span_start",
            "span_end": "log_span_end",
            "rule_eval": "log_rule_eval",
        }
        updates = {}
        for old_key, new_key in mapping.items():
            if old_key in config_dict:
                value = bool(config_dict[old_key])
                updates[new_key] = value
                setattr(self, old_key, value)

        if updates:
            configure_settings(**updates)


# Global logging configuration
_log_config = LogConfig.from_env()


def configure_logging(config: dict[str, Any] | None = None) -> LogConfig:
    """
    Configure SDK logging behavior (which categories of logs to emit).

    This Rules which types of logs the SDK emits, not where they go or
    what level they're logged at. For log level/handler configuration, use
    Python's standard logging module.

    Can be called programmatically to override environment variable defaults.

    Args:
        config: Dictionary with logging options:
            - enabled: bool - Master switch for all SDK logging (default: True)
            - level: str - Use logging.setLevel() instead
            - span_start: bool - Emit span start logs (default: True)
            - span_end: bool - Emit span end logs (default: True)
            - rule_eval: bool - Emit per-rule check logs (default: True)

    Returns:
        Current LogConfig after applying changes

    Example:
        # Select which SDK logs are emitted
        configure_logging({
            "enabled": True,
            "rule_eval": False,  # Don't emit per-rule logs
        })

        # For log levels, use standard Python logging
        import logging
        logging.getLogger('norma').setLevel(logging.DEBUG)
    """
    global _log_config
    if config:
        _log_config.update(config)
    return _log_config


def get_log_config() -> LogConfig:
    """Get the current logging configuration."""
    return _log_config


def _should_log(log_type: str) -> bool:
    """
    Check if a specific log type should be emitted by the SDK.

    This Rules which categories of logs the SDK emits, independent of
    Python's logging level filtering. Applications Rule actual log levels
    via logging.getLogger('norma').setLevel().

    Args:
        log_type: Type of log ("span_start", "span_end", "rule_eval")

    Returns:
        True if this type of log should be emitted
    """
    if not get_settings().log_enabled:
        return False

    if log_type == "span_start":
        return get_settings().log_span_start
    elif log_type == "span_end":
        return get_settings().log_span_end
    elif log_type == "rule_eval":
        return get_settings().log_rule_eval

    return True


# =============================================================================
# Event Batching Configuration (now via settings)
# =============================================================================


@dataclass(frozen=True)
class DeliveryReceipt:
    """Snapshot of event delivery for this SDK session, separate from Rule decisions.

    ``acknowledged`` counts complete batches accepted by ingestion. ``persisted``
    counts those events confirmed stored or already present by an idempotent replay.
    ``pending`` includes queued and in-flight events; partial batches remain pending
    in full because the response does not identify which events need retrying.
    ``dropped`` counts events the SDK can no longer retry, not proven server losses.
    Custom sinks cannot confirm records in Norma and return ``unconfirmed``.
    """

    status: Literal[
        "delivered", "acknowledged", "pending", "failed", "empty", "disabled", "unconfirmed"
    ]
    acknowledged: int = 0
    persisted: int = 0
    pending: int = 0
    dropped: int = 0
    message: str | None = None


class EventBatcher:
    """
    Batches rule check events and sends them to the server.

    Events are batched by either:
    - Reaching batch_size events (default: 100)
    - Flush interval timeout (default: 5 seconds)

    Uses a dedicated daemon thread with its own event loop for flush
    scheduling, so it works consistently regardless of caller loop
    lifecycle (sync callers, repeated asyncio.run(), long-lived async
    servers, etc.).

    Thread-safe. add_event() is non-blocking and can be called from
    any thread or async context.

    Attributes:
        runtime_url: Base URL of the Norma server
        agent_key: API key for authentication
        batch_size: Maximum events per batch
        flush_interval: Seconds between automatic flushes
    """

    def __init__(
        self,
        runtime_url: str | None = None,
        agent_key: str | None = None,
        batch_size: int | None = None,
        flush_interval: float | None = None,
    ) -> None:
        """
        Initialize the EventBatcher.

        Args:
            runtime_url: Server URL (defaults to get_settings().runtime_url)
            agent_key: API key (defaults to get_settings().agent_key)
            batch_size: Max events per batch (defaults to get_settings().batch_size)
            flush_interval: Seconds between flushes (defaults to get_settings().flush_interval)
        """
        self.runtime_url = (
            runtime_url if runtime_url is not None else get_settings().runtime_url
        ).rstrip("/")
        self.agent_key = agent_key if agent_key is not None else get_settings().agent_key
        self.batch_size = batch_size if batch_size is not None else get_settings().batch_size
        if flush_interval is not None:
            self.flush_interval = flush_interval
        else:
            self.flush_interval = get_settings().flush_interval
        self.shutdown_join_timeout = get_settings().shutdown_join_timeout
        self.shutdown_flush_timeout = get_settings().shutdown_flush_timeout
        self.shutdown_max_failed_flushes = get_settings().shutdown_max_failed_flushes

        # Thread-safe event storage
        self._events: list[RuleCheckEvent] = []
        self._lock = threading.Lock()

        # Dedicated worker loop and thread
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._flush_signal: asyncio.Event | None = None
        self._worker_ready = threading.Event()
        self._graceful_shutdown = False
        self._running = False
        self._closed = False

        # Reusable HTTP client for connection pooling
        self._client: httpx.AsyncClient | None = None

        # Stats
        self._events_sent = 0
        self._events_persisted = 0
        self._events_in_flight = 0
        self._last_delivery_error: str | None = None
        self._async_flush_lock: asyncio.Lock | None = None
        self._async_flush_lock_loop: asyncio.AbstractEventLoop | None = None
        self._events_dropped = 0
        self._flush_count = 0

    def start(self) -> None:
        """Start the dedicated worker thread and flush loop."""
        if self._running:
            return

        self._running = True
        self._graceful_shutdown = False
        self._worker_ready.clear()
        self._thread = threading.Thread(
            target=self._run_worker_loop,
            name="norma-event-batcher",
            daemon=True,
        )
        self._thread.start()
        if not self._worker_ready.wait(timeout=1.0):
            logger.warning("EventBatcher worker thread did not signal readiness in time")
        logger.debug("EventBatcher started (dedicated worker thread)")

    def _run_worker_loop(self) -> None:
        """Entry point for the worker thread. Runs the event loop."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._flush_signal = asyncio.Event()
        self._worker_ready.set()
        try:
            loop.run_until_complete(self._flush_loop())
        except RuntimeError:
            # Can happen if emergency stop is requested while loop is running
            pass
        finally:
            # Emergency loop.stop() can leave a send suspended. Give cancellation
            # a bounded opportunity to requeue its batch before fallback shutdown.
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                try:
                    loop.run_until_complete(asyncio.wait(tasks, timeout=0.25))
                except RuntimeError:
                    pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except RuntimeError:
                pass
            loop.close()
            self._flush_signal = None
            self._loop = None
            self._worker_ready.clear()

    def _signal_flush(self) -> None:
        """Wake the worker loop to perform (or finish) a flush cycle."""
        loop = self._loop
        signal = self._flush_signal
        if loop is None or signal is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(signal.set)
        except RuntimeError:
            # Expected during shutdown races: worker teardown can close/swap
            # loop state between our check and call_soon_threadsafe().
            pass

    def _stop_worker(self, *, graceful: bool, join_timeout: float) -> bool:
        """Stop the worker thread and return whether it fully stopped."""
        self._graceful_shutdown = graceful
        self._running = False
        self._signal_flush()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)

        if self._thread and self._thread.is_alive():
            # Emergency fallback if worker is still hung.
            loop = self._loop
            if loop and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass
            self._thread.join(timeout=1.0)

        thread_alive = self._thread.is_alive() if self._thread else False
        if thread_alive:
            logger.warning("EventBatcher worker thread did not stop cleanly")
            return False
        self._thread = None
        return True

    def _build_batch_request(
        self,
        events: list[RuleCheckEvent],
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Build request components shared by async and sync send paths."""
        url = f"{self.runtime_url}/api/v1/observability/events"
        headers = {"Content-Type": "application/json"}
        if self.agent_key:
            headers["X-API-Key"] = self.agent_key
        payload = {"events": [event._to_wire() for event in events]}
        return url, headers, payload

    def _delivery_failed(self, message: str) -> None:
        """Record safe guidance without exposing a server body or credentials."""
        with self._lock:
            self._last_delivery_error = message
        logger.warning(message)

    def _accept_batch_response(self, response: httpx.Response, count: int) -> bool:
        """Require a complete, internally consistent ingestion acknowledgement."""
        if response.status_code != 202:
            if response.status_code in (401, 403):
                message = (
                    "Norma could not authenticate event delivery. Check the configured agent "
                    "key and its agent before retrying."
                )
            else:
                message = (
                    f"Norma event ingestion returned HTTP {response.status_code}. "
                    "Check server availability and retry flushing."
                )
            self._delivery_failed(message)
            return False
        try:
            receipt = BatchEventsResponse.model_validate(response.json(), strict=True)
            valid_counts = (
                receipt.received == count
                and receipt.enqueued + receipt.dropped == count
                and receipt.duplicates <= receipt.enqueued
                and (
                    receipt.stored is None
                    or receipt.stored + receipt.duplicates == receipt.enqueued
                )
            )
            if not valid_counts:
                raise ValueError("Inconsistent ingestion counts")
        except (ValueError, TypeError):
            self._delivery_failed(
                "Norma returned an invalid event receipt; delivery is unconfirmed. "
                "Check SDK/server compatibility and retry flushing."
            )
            return False
        if receipt.status != "queued" or receipt.enqueued != count or receipt.dropped:
            self._delivery_failed(
                "Norma did not acknowledge the complete event batch. "
                "Check server storage and retry flushing; event IDs make retries idempotent."
            )
            return False
        with self._lock:
            if self._closed:
                return False
            if receipt.stored is not None:
                self._events_persisted += count
            self._last_delivery_error = None
        return True

    def _send_batch_sync(
        self,
        events: list[RuleCheckEvent],
        *,
        deadline: float | None = None,
    ) -> bool:
        """Send a batch synchronously for shutdown/atexit fallback."""
        url, headers, payload = self._build_batch_request(events)

        for attempt in range(get_settings().max_retries):
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._delivery_failed("Norma event flush timed out; retry before exiting.")
                    return False

            request_timeout = 30.0
            if remaining is not None:
                request_timeout = max(0.001, min(request_timeout, remaining))

            try:
                with httpx.Client(timeout=request_timeout) as client:
                    response = client.post(url, json=payload, headers=headers)

                if self._accept_batch_response(response, len(events)):
                    return True
                if response.status_code in (401, 403):
                    return False
            except httpx.TimeoutException:
                self._delivery_failed(
                    "Norma event delivery timed out; check connectivity and retry."
                )
            except httpx.ConnectError:
                self._delivery_failed(
                    "Norma event delivery could not connect; check the server URL and retry."
                )
            except Exception:
                self._delivery_failed("Norma event delivery failed; check configuration and retry.")

            if attempt >= get_settings().max_retries - 1:
                break

            retry_delay = get_settings().retry_delay * (attempt + 1)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("Fallback shutdown flush timed out during retry backoff")
                    return False
                retry_delay = min(retry_delay, remaining)

            if retry_delay > 0:
                time.sleep(retry_delay)

        return False

    def _flush_sync(self, *, deadline: float | None = None) -> bool:
        """Flush a batch synchronously without relying on asyncio state."""
        with self._lock:
            if not self._events:
                return True
            events_to_send = self._events[: self.batch_size]
            self._events = self._events[self.batch_size :]
            self._events_in_flight += len(events_to_send)

        success = self._send_batch_sync(events_to_send, deadline=deadline)
        with self._lock:
            self._events_in_flight = max(0, self._events_in_flight - len(events_to_send))
            if self._closed:
                return False
            if success:
                self._events_sent += len(events_to_send)
                self._flush_count += 1
            else:
                self._events = events_to_send + self._events
        if not success:
            logger.warning("Failed to confirm batch, re-queued %d events", len(events_to_send))
        return success

    def _flush_all_without_worker(self, *, timeout: float) -> None:
        """Flush remaining events synchronously when no worker loop is available."""
        deadline = time.monotonic() + max(timeout, 0.0)
        consecutive_failures = 0

        while True:
            with self._lock:
                if not self._events:
                    break

            if time.monotonic() >= deadline:
                logger.warning("Fallback shutdown flush timed out after %.1f seconds", timeout)
                break

            flushed = self._flush_sync(deadline=deadline)
            if flushed:
                consecutive_failures = 0
                continue

            consecutive_failures += 1
            if consecutive_failures >= self.shutdown_max_failed_flushes:
                with self._lock:
                    pending = len(self._events)
                logger.warning(
                    "Stopping sync shutdown flush after %d consecutive failed flushes; "
                    "%d event(s) pending",
                    consecutive_failures,
                    pending,
                )
                break

        # The worker thread normally closes the async client. If shutdown races or
        # interpreter teardown prevented that path from completing, we intentionally
        # do not try to aclose() here because this fallback may be running after
        # asyncio teardown. Drop the reference so shutdown can finish deterministically.
        self._client = None

    def stop(self) -> None:
        """Stop the worker thread. Does not flush remaining events."""
        self._stop_worker(graceful=False, join_timeout=2.0)
        logger.debug("EventBatcher stopped")

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def add_event(self, event: RuleCheckEvent) -> bool:
        """
        Add an event to the batch.

        Thread-safe and non-blocking. Can be called from any thread or
        async context.

        Args:
            event: Rule check event to add

        Returns:
            True if event was added, False if dropped (e.g., queue full)
        """
        should_flush = False
        with self._lock:
            if self._closed:
                self._events_dropped += 1
                logger.warning("Norma event dropped: batcher is shut down")
                return False
            if len(self._events) >= self.batch_size * 10:
                self._events_dropped += 1
                logger.warning("Event dropped: queue full")
                return False

            self._events.append(event)
            should_flush = len(self._events) >= self.batch_size

        if should_flush:
            self._schedule_flush()

        return True

    def _schedule_flush(self) -> None:
        """Schedule an immediate flush on the worker loop (non-blocking)."""
        if not self._running:
            return
        self._signal_flush()

    async def _flush_loop(self) -> None:
        """Background task that flushes events periodically."""
        signal = self._flush_signal
        if signal is None:
            logger.error("Flush loop started without a worker flush signal")
            return

        while self._running:
            try:
                await asyncio.wait_for(signal.wait(), timeout=self.flush_interval)
                signal.clear()
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Flush loop wakeup error: {e}")
                continue

            if not self._running:
                break

            try:
                await self._flush()
            except Exception as e:
                logger.error(f"Flush loop error: {e}")

        try:
            if self._graceful_shutdown:
                await self.flush_all(
                    close_client=False,
                    max_failed_flushes=self.shutdown_max_failed_flushes,
                )
        except Exception as e:
            logger.error(f"Error during flush loop shutdown: {e}")
        finally:
            try:
                await self.close()
            except Exception as e:
                logger.error(f"Error closing observability client: {e}")

    async def _flush(self) -> bool:
        """Flush one batch, serializing callers on the worker's event loop."""
        loop = asyncio.get_running_loop()
        if self._async_flush_lock_loop is not loop:
            self._async_flush_lock = asyncio.Lock()
            self._async_flush_lock_loop = loop
        assert self._async_flush_lock is not None
        async with self._async_flush_lock:
            with self._lock:
                if not self._events:
                    return True
                events_to_send = self._events[: self.batch_size]
                self._events = self._events[self.batch_size :]
                self._events_in_flight += len(events_to_send)

            success = False
            try:
                success = await self._send_batch(events_to_send)
            finally:
                with self._lock:
                    self._events_in_flight = max(0, self._events_in_flight - len(events_to_send))
                    if not self._closed:
                        if success:
                            self._events_sent += len(events_to_send)
                            self._flush_count += 1
                        else:
                            self._events = events_to_send + self._events
            if not success:
                logger.warning("Failed to confirm batch, re-queued %d events", len(events_to_send))
            return success

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client for connection pooling."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _send_batch(self, events: list[RuleCheckEvent]) -> bool:
        """
        Send a batch of events to the server.

        Args:
            events: List of events to send

        Returns:
            True if sent successfully, False otherwise
        """
        url, headers, payload = self._build_batch_request(events)

        client = await self._get_client()

        for attempt in range(get_settings().max_retries):
            try:
                response = await client.post(url, json=payload, headers=headers)

                if self._accept_batch_response(response, len(events)):
                    return True
                if response.status_code in (401, 403):
                    return False

            except httpx.TimeoutException:
                self._delivery_failed(
                    "Norma event delivery timed out; check connectivity and retry."
                )
            except httpx.ConnectError:
                self._delivery_failed(
                    "Norma event delivery could not connect; check the server URL and retry."
                )
            except Exception:
                self._delivery_failed("Norma event delivery failed; check configuration and retry.")

            if attempt < get_settings().max_retries - 1:
                await asyncio.sleep(get_settings().retry_delay * (attempt + 1))

        return False

    async def flush_all(
        self,
        *,
        close_client: bool = True,
        max_failed_flushes: int | None = None,
    ) -> DeliveryReceipt:
        """Flush pending events and return a cumulative delivery receipt.

        Stops after ``max_failed_flushes`` consecutive failures; uncertain batches
        stay queued for retry. Rule enforcement does not depend on this receipt.
        """
        worker_loop = self._loop
        if worker_loop is not None and worker_loop is not asyncio.get_running_loop():
            future = asyncio.run_coroutine_threadsafe(
                self.flush_all(close_client=False, max_failed_flushes=max_failed_flushes),
                worker_loop,
            )
            return await asyncio.wrap_future(future)
        failure_limit = (
            max_failed_flushes
            if max_failed_flushes is not None
            else self.shutdown_max_failed_flushes
        )
        if failure_limit < 1:
            raise ValueError("max_failed_flushes must be >= 1")
        consecutive_failures = 0

        while True:
            with self._lock:
                if not self._events and not self._events_in_flight:
                    break

            flushed = await self._flush()
            if flushed:
                consecutive_failures = 0
                continue

            consecutive_failures += 1
            if consecutive_failures >= failure_limit:
                with self._lock:
                    pending = len(self._events)
                logger.warning(
                    "Stopping flush_all after %d consecutive failed flushes; %d event(s) pending",
                    consecutive_failures,
                    pending,
                )
                break

        if close_client:
            await self.close()
        return self.get_delivery_receipt()

    def shutdown(self) -> DeliveryReceipt:
        """
        Synchronous shutdown - flush remaining events and join worker thread.

        Called on process exit via atexit. Returns a cumulative receipt; events
        still unconfirmed at the shutdown deadline are reported as dropped.
        """
        with self._lock:
            initial_remaining = len(self._events)
        if initial_remaining > 0:
            logger.info(f"Flushing {initial_remaining} remaining events on shutdown...")

        worker_stopped = self._stop_worker(
            graceful=True,
            join_timeout=self.shutdown_join_timeout,
        )

        with self._lock:
            needs_fallback_flush = bool(self._events)
        if self._client is not None:
            needs_fallback_flush = True
        if needs_fallback_flush:
            if worker_stopped:
                self._flush_all_without_worker(timeout=self.shutdown_flush_timeout)
            else:
                logger.warning(
                    "Skipping fallback shutdown flush because worker thread is still running"
                )

        with self._lock:
            remaining = len(self._events) + self._events_in_flight
            if remaining > 0:
                self._events_dropped += remaining
                logger.warning("Dropped %d unconfirmed events during shutdown", remaining)
            self._events.clear()
            self._events_in_flight = 0
            self._closed = True
            events_sent = self._events_sent
            events_dropped = self._events_dropped
            flush_count = self._flush_count

        logger.info(
            f"EventBatcher shutdown: sent={events_sent}, "
            f"dropped={events_dropped}, flushes={flush_count}"
        )
        return self.get_delivery_receipt()

    def get_delivery_receipt(self) -> DeliveryReceipt:
        """Return a truthful snapshot without waiting for delivery."""
        with self._lock:
            pending = len(self._events) + self._events_in_flight
            acknowledged = self._events_sent
            persisted = min(self._events_persisted, acknowledged)
            dropped = self._events_dropped
            error = self._last_delivery_error
        status: Literal[
            "delivered", "acknowledged", "pending", "failed", "empty", "disabled", "unconfirmed"
        ]
        if dropped:
            status = "failed"
            message = (
                f"{dropped} event(s) could not be confirmed before the SDK stopped retrying. "
                + (error or "Check connectivity and flush events before shutdown.")
            )
        elif pending:
            status = "pending"
            message = error or "Events are queued or in flight. Flush again to confirm delivery."
        elif acknowledged and persisted == acknowledged:
            status = "delivered"
            message = "Records confirmed stored. Open this agent in Norma, then Checks."
        elif acknowledged:
            status = "acknowledged"
            message = (
                "Ingestion acknowledged the events without confirming storage. "
                "Check the selected sink and this agent's Checks in Norma."
            )
        else:
            status = "empty"
            message = (
                "No Rule checks were queued. Attach an enabled Rule to the agent and "
                "run a matching instrumented checkpoint."
            )
        return DeliveryReceipt(status, acknowledged, persisted, pending, dropped, message)

    def get_stats(self) -> dict:
        """Get batcher statistics."""
        with self._lock:
            pending = len(self._events) + self._events_in_flight
            events_sent = self._events_sent
            events_dropped = self._events_dropped
            flush_count = self._flush_count
            running = self._running
        return {
            "events_sent": events_sent,
            "events_dropped": events_dropped,
            "events_pending": pending,
            "flush_count": flush_count,
            "running": running,
        }


# Global batcher instance
_batcher: EventBatcher | None = None
_event_sink: RuleEventSink | None = None
_external_event_sinks: list[RuleEventSink] = []
_external_event_sinks_lock = threading.Lock()
_named_event_sink_factories: RuleEventSinkFactoryRegistry[RuleEventSink] = (
    RuleEventSinkFactoryRegistry()
)
_configured_named_event_sink: RuleEventSink | None = None
_configured_named_event_sink_selection: RuleEventSinkSelection | None = None
_configured_otel_tracer_provider: TracerProvider | None = None
_configured_named_event_sink_provider: TracerProvider | None = None
_configured_named_event_sink_lock = threading.Lock()
_used_custom_event_sinks: list[RuleEventSink] = []
_used_custom_event_sinks_lock = threading.Lock()
_custom_events_dropped = 0
_custom_delivery_lock = threading.Lock()


def _register_builtin_rule_event_sink_factories() -> None:
    """Ensure built-in named sink factories are available."""
    _named_event_sink_factories.register(
        OTEL_RULE_EVENT_SINK_NAME,
        _create_configured_otel_rule_event_sink,
    )


def _create_configured_otel_rule_event_sink(config: JSONObject) -> BaseRuleEventSink:
    """Create the OTEL sink with its runtime-only provider configuration."""
    return create_otel_rule_event_sink(
        config,
        tracer_provider=_configured_otel_tracer_provider,
    )


_register_builtin_rule_event_sink_factories()


class _BatcherRuleEventSink(BaseRuleEventSink):
    """Default SDK sink backed by the existing queue-based EventBatcher."""

    def __init__(self, batcher: EventBatcher):
        self._batcher = batcher

    def write_events(self, events: Sequence[RuleCheckEvent]) -> SinkResult:
        accepted = 0
        dropped = 0
        for event in events:
            if self._batcher.add_event(event):
                accepted += 1
            else:
                dropped += 1
        return SinkResult(accepted=accepted, dropped=dropped)


def get_event_batcher() -> EventBatcher | None:
    """
    Get the global EventBatcher instance.

    Returns:
        EventBatcher if observability is enabled, None otherwise
    """
    return _batcher


def get_event_sink() -> RuleEventSink | None:
    """Get the active built-in rule-event sink."""
    return _event_sink


def register_rule_event_sink(sink: RuleEventSink) -> None:
    """Register an external rule-event sink.

    Registered sinks receive the same finalized rule-event payloads emitted
    through the SDK's hosted check event flow when the active
    sink selection is `registered`. Registration is idempotent for the same
    sink instance. Caller-registered sinks remain caller-owned: the SDK does
    not flush, close, or unregister them during shutdown.
    """
    with _external_event_sinks_lock:
        if not any(registered_sink is sink for registered_sink in _external_event_sinks):
            _external_event_sinks.append(sink)


def unregister_rule_event_sink(sink: RuleEventSink) -> None:
    """Unregister a previously registered external rule-event sink."""
    with _external_event_sinks_lock:
        for index, registered_sink in enumerate(_external_event_sinks):
            if registered_sink is sink:
                del _external_event_sinks[index]
                break


def get_registered_rule_event_sinks() -> tuple[RuleEventSink, ...]:
    """Return the currently registered external rule-event sinks."""
    with _external_event_sinks_lock:
        return tuple(_external_event_sinks)


def register_rule_event_sink_factory(
    name: str,
    factory: RuleEventSinkFactory[RuleEventSink],
) -> None:
    """Register a named rule-event sink factory for config-based selection."""
    global _configured_named_event_sink, _configured_named_event_sink_selection

    _named_event_sink_factories.register(name, factory)
    with _configured_named_event_sink_lock:
        _configured_named_event_sink = None
        _configured_named_event_sink_selection = None


def unregister_rule_event_sink_factory(name: str) -> None:
    """Unregister a previously registered named rule-event sink factory."""
    global _configured_named_event_sink, _configured_named_event_sink_selection

    _named_event_sink_factories.unregister(name)
    with _configured_named_event_sink_lock:
        _configured_named_event_sink = None
        _configured_named_event_sink_selection = None


def get_registered_rule_event_sink_factory_names() -> tuple[str, ...]:
    """Return the registered named rule-event sink factories."""
    return _named_event_sink_factories.registered_names()


def _remember_custom_rule_event_sinks(sinks: Sequence[RuleEventSink]) -> None:
    """Track custom sink instances that should be cleaned up on shutdown."""
    with _used_custom_event_sinks_lock:
        for sink in sinks:
            if not any(remembered_sink is sink for remembered_sink in _used_custom_event_sinks):
                _used_custom_event_sinks.append(sink)


def _sink_is_active(sink: RuleEventSink) -> bool:
    """Return whether a sink instance is currently able to deliver events."""
    is_active = getattr(sink, "is_active", None)
    if callable(is_active):
        return bool(is_active())
    return True


def _get_sink_selection() -> RuleEventSinkSelection:
    """Build the current sink-selection model from SDK settings."""
    settings = get_settings()
    config: JSONObject = dict(settings.observability_sink_config or {})
    if settings.observability_sink_name == OTEL_RULE_EVENT_SINK_NAME:
        # Materialize OTEL-specific settings into the selection so that
        # changes to otel_endpoint / otel_headers / otel_service_name /
        # otel_enabled invalidate the cached sink instance.
        config.setdefault("enabled", settings.otel_enabled)
        config.setdefault("endpoint", settings.otel_endpoint)
        config.setdefault("headers", dict(settings.otel_headers))
        config.setdefault("service_name", settings.otel_service_name)
    return RuleEventSinkSelection(
        name=settings.observability_sink_name,
        config=config,
    )


def _get_or_create_named_rule_event_sink(
    selection: RuleEventSinkSelection,
) -> RuleEventSink | None:
    """Resolve and cache a named configured sink."""
    global _configured_named_event_sink, _configured_named_event_sink_provider
    global _configured_named_event_sink_selection

    previous_sink: RuleEventSink | None = None
    selected_provider = (
        _configured_otel_tracer_provider if selection.name == OTEL_RULE_EVENT_SINK_NAME else None
    )

    with _configured_named_event_sink_lock:
        if (
            _configured_named_event_sink is not None
            and _configured_named_event_sink_selection == selection
            and _configured_named_event_sink_provider is selected_provider
        ):
            return _configured_named_event_sink

        try:
            sink = _named_event_sink_factories.resolve(selection)
        except SinkSelectionError:
            logger.warning("Configured rule-event sink '%s' is not available", selection.name)
            return None
        except Exception:
            logger.warning(
                "Configured rule-event sink '%s' failed to initialize",
                selection.name,
                exc_info=True,
            )
            return None

        if _configured_named_event_sink is not None and _configured_named_event_sink is not sink:
            previous_sink = _configured_named_event_sink
        _configured_named_event_sink = sink
        _configured_named_event_sink_selection = selection.model_copy(deep=True)
        _configured_named_event_sink_provider = selected_provider

    if previous_sink is not None:
        _shutdown_custom_rule_event_sink(previous_sink)

    return sink


def _get_active_rule_event_sinks() -> tuple[RuleEventSink, ...]:
    """Resolve the currently active sinks.

    Observability must be enabled before any sink is considered. When enabled,
    config selects the default built-in sink, the registered external sink set,
    or a named registered sink factory.
    """
    if not get_settings().observability_enabled:
        return ()

    selection = _get_sink_selection()
    if selection.name == DEFAULT_RULE_EVENT_SINK_NAME:
        if _event_sink is None or not _sink_is_active(_event_sink):
            return ()
        return (_event_sink,)
    if selection.name == REGISTERED_RULE_EVENT_SINK_NAME:
        sinks = get_registered_rule_event_sinks()
        sinks = tuple(sink for sink in sinks if _sink_is_active(sink))
        _remember_custom_rule_event_sinks(sinks)
        return sinks

    named_sink = _get_or_create_named_rule_event_sink(selection)
    if named_sink is None or not _sink_is_active(named_sink):
        return ()
    _remember_custom_rule_event_sinks((named_sink,))
    return (named_sink,)


def _shutdown_built_in_event_sink() -> DeliveryReceipt | None:
    """Stop and clear the built-in batcher, preserving its delivery receipt."""
    global _batcher, _event_sink

    if _batcher is not None:
        receipt = _batcher.shutdown()
        atexit.unregister(_batcher.shutdown)
    else:
        receipt = None
    _batcher = None
    _event_sink = None
    return receipt


def _shutdown_configured_named_event_sink() -> bool:
    """Stop and clear the cached configured named sink if it is active."""
    global _configured_named_event_sink, _configured_named_event_sink_provider
    global _configured_named_event_sink_selection

    configured_named_sink: RuleEventSink | None = None
    with _configured_named_event_sink_lock:
        configured_named_sink = _configured_named_event_sink
        _configured_named_event_sink = None
        _configured_named_event_sink_selection = None
        _configured_named_event_sink_provider = None

    if configured_named_sink is not None:
        return _shutdown_custom_rule_event_sink(configured_named_sink)
    return True


def _invalidate_cached_otel_sink() -> None:
    """Clear and shut down only the cached built-in OTEL sink, if present."""
    global _configured_named_event_sink, _configured_named_event_sink_provider
    global _configured_named_event_sink_selection

    configured_otel_sink: RuleEventSink | None = None
    with _configured_named_event_sink_lock:
        if (
            _configured_named_event_sink_selection is None
            or _configured_named_event_sink_selection.name != OTEL_RULE_EVENT_SINK_NAME
        ):
            return
        configured_otel_sink = _configured_named_event_sink
        _configured_named_event_sink = None
        _configured_named_event_sink_selection = None
        _configured_named_event_sink_provider = None

    if configured_otel_sink is not None:
        _shutdown_custom_rule_event_sink(configured_otel_sink)


def _flush_custom_rule_event_sink(sink: RuleEventSink) -> bool:
    """Flush a custom sink, retaining explicit lifecycle failure."""
    flush = getattr(sink, "flush", None)
    if callable(flush):
        try:
            result = flush()
            if inspect.isawaitable(result):
                result = asyncio.run(_run_awaitable_during_shutdown(result))
            if result is False:
                logger.warning("Norma event sink could not finish flushing")
                return False
        except Exception:
            logger.warning("Norma event sink flush failed; check exporter configuration")
            return False
    return True


def _shutdown_custom_rule_event_sink(sink: RuleEventSink) -> bool:
    """Flush and close an owned custom sink, retaining lifecycle failure."""
    success = _flush_custom_rule_event_sink(sink)
    for method_name in ("close", "shutdown"):
        callback = getattr(sink, method_name, None)
        if not callable(callback):
            continue
        try:
            result = callback()
            if inspect.isawaitable(result):
                result = asyncio.run(_run_awaitable_during_shutdown(result))
            if result is False:
                success = False
        except Exception:
            logger.warning("Norma event sink %s failed during shutdown", method_name)
            success = False
        break
    return success


async def _run_awaitable_during_shutdown(result: Awaitable[Any]) -> Any:
    """Await a sink lifecycle callback through a concrete coroutine."""
    return await result


def _get_custom_rule_event_sinks_to_shutdown() -> tuple[RuleEventSink, ...]:
    """Collect custom sink instances that should be cleaned up on shutdown."""
    with _used_custom_event_sinks_lock:
        return tuple(_used_custom_event_sinks)


def init_observability(
    runtime_url: str | None = None,
    agent_key: str | None = None,
    enabled: bool | None = None,
    sink_name: str | None = None,
    sink_config: JSONObject | None = None,
    otel_tracer_provider: TracerProvider | None = None,
    *,
    new_session: bool = False,
) -> EventBatcher | None:
    """
    Initialize observability system.

    Called automatically by norma.init() if observability is enabled.

    Args:
        runtime_url: Server URL for sending events
        agent_key: API key for authentication
        enabled: Override NORMA_OBSERVABILITY_ENABLED
        sink_name: Override NORMA_OBSERVABILITY_SINK_NAME
        sink_config: Override NORMA_OBSERVABILITY_SINK_CONFIG
        otel_tracer_provider: Runtime-only provider for the built-in OTEL sink
        new_session: Start fresh receipt counters even when the connection is unchanged.
            The public init/ainit lifecycle uses this for each successful registration.

    Returns:
        EventBatcher instance if enabled, None otherwise
    """
    global _batcher, _configured_otel_tracer_provider, _event_sink, _custom_events_dropped

    with _custom_delivery_lock:
        _custom_events_dropped = 0
    settings_updates: dict[str, object] = {}
    current_settings = get_settings()
    if enabled is not None:
        settings_updates["observability_enabled"] = enabled
    if sink_name is not None:
        settings_updates["observability_sink_name"] = sink_name
        if sink_config is None and sink_name != current_settings.observability_sink_name:
            settings_updates["observability_sink_config"] = {}
    if sink_config is not None:
        settings_updates["observability_sink_config"] = sink_config
    if settings_updates:
        configure_settings(**settings_updates)

    selected_sink_name = get_settings().observability_sink_name
    selected_otel_tracer_provider = (
        otel_tracer_provider if selected_sink_name == OTEL_RULE_EVENT_SINK_NAME else None
    )
    provider_changed = selected_otel_tracer_provider is not _configured_otel_tracer_provider
    _configured_otel_tracer_provider = selected_otel_tracer_provider
    if provider_changed:
        _invalidate_cached_otel_sink()

    is_enabled = get_settings().observability_enabled

    if not is_enabled:
        logger.debug("Observability disabled")
        _shutdown_built_in_event_sink()
        _shutdown_configured_named_event_sink()
        return None

    selection = _get_sink_selection()
    if selection.name != DEFAULT_RULE_EVENT_SINK_NAME:
        logger.info(
            "Observability initialized with configured sink '%s' (default batcher disabled)",
            selection.name,
        )
        _shutdown_built_in_event_sink()
        return None

    if _event_sink is not None and _batcher is not None:
        settings = get_settings()
        desired_connection = (
            (runtime_url if runtime_url is not None else settings.runtime_url).rstrip("/"),
            agent_key if agent_key is not None else settings.agent_key,
        )
        current_connection = (_batcher.runtime_url, _batcher.agent_key)
        if desired_connection == current_connection and not new_session:
            logger.debug("Observability already initialized")
            return _batcher
        # Flush the prior session with its own credentials before replacing it.
        previous = _shutdown_built_in_event_sink()
        if previous is not None and previous.status in ("failed", "pending"):
            logger.warning("Previous Norma session has unconfirmed event delivery")

    # Create batcher
    _batcher = EventBatcher(
        runtime_url=runtime_url,
        agent_key=agent_key,
    )
    _batcher.start()
    _event_sink = _BatcherRuleEventSink(_batcher)

    # Register shutdown handler
    atexit.register(_batcher.shutdown)

    logger.info("Observability initialized")
    return _batcher


def add_event(event: RuleCheckEvent) -> bool:
    """
    Add an event to the active rule-event sink.

    Args:
        event: Rule check event to add

    Returns:
        True if added, False if observability disabled or event dropped
    """
    return write_events([event]).accepted == 1


def write_events(events: Sequence[RuleCheckEvent]) -> SinkResult:
    """Write events through the active sink selection.

    ``accepted`` means accepted into each selected sink, which may only be a
    local queue. It does not confirm persistence. Use flush or shutdown receipts
    to inspect delivery. Fanout reports the least successful sink's result.
    """
    global _custom_events_dropped
    active_sinks = _get_active_rule_event_sinks()
    if not active_sinks:
        if get_settings().observability_sink_name != DEFAULT_RULE_EVENT_SINK_NAME:
            with _custom_delivery_lock:
                _custom_events_dropped += len(events)
        return SinkResult(accepted=0, dropped=len(events))

    min_accepted = len(events)
    max_dropped = 0

    for sink in active_sinks:
        try:
            result = sink.write_events(events)
        except Exception:
            logger.warning("Rule-event sink write failed", exc_info=True)
            min_accepted = 0
            max_dropped = max(max_dropped, len(events))
            continue

        min_accepted = min(min_accepted, result.accepted)
        max_dropped = max(max_dropped, result.dropped)

    dropped = max(max_dropped, len(events) - min_accepted)
    accepted = min(min_accepted, max(len(events) - dropped, 0))
    if get_settings().observability_sink_name != DEFAULT_RULE_EVENT_SINK_NAME:
        with _custom_delivery_lock:
            _custom_events_dropped += dropped
    return SinkResult(accepted=accepted, dropped=dropped)


def _inactive_delivery_receipt() -> DeliveryReceipt:
    if not get_settings().observability_enabled:
        return DeliveryReceipt(
            "disabled", message="Event recording is disabled. Enable NORMA_OBSERVABILITY_ENABLED."
        )
    return DeliveryReceipt(
        "empty",
        message=(
            "No event batcher is active. Initialize Norma, attach an enabled Rule, "
            "and run a matching instrumented checkpoint."
        ),
    )


def _custom_delivery_receipt(success: bool) -> DeliveryReceipt:
    with _custom_delivery_lock:
        dropped = _custom_events_dropped
    success = success and dropped == 0
    return DeliveryReceipt(
        "unconfirmed" if success else "failed",
        dropped=dropped,
        message=(
            "The selected sink does not confirm records stored in Norma. "
            "Inspect its exporter or use the default Norma sink."
            if success
            else "The selected event sink dropped events or failed to flush or close. "
            "Check its exporter configuration and retry before exit."
        ),
    )


def sync_flush_observability() -> DeliveryReceipt:
    """Flush without shutting down and return a cumulative delivery receipt.

    This operation never changes a Rule's enforcement result. Partial or failed
    default-sink deliveries remain queued; callers can retry this operation.
    """
    batcher = _batcher
    if batcher is not None:
        loop = batcher._loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                batcher.flush_all(close_client=False),
                loop,
            )
            try:
                return future.result(timeout=batcher.shutdown_flush_timeout)
            except TimeoutError:
                future.cancel()
                batcher._delivery_failed("Norma event flush timed out; retry before exiting.")
                return batcher.get_delivery_receipt()
        batcher._flush_all_without_worker(timeout=batcher.shutdown_flush_timeout)
        return batcher.get_delivery_receipt()
    if not get_settings().observability_enabled:
        return _inactive_delivery_receipt()
    if get_settings().observability_sink_name != DEFAULT_RULE_EVENT_SINK_NAME:
        sinks = _get_active_rule_event_sinks()
        results = [_flush_custom_rule_event_sink(sink) for sink in sinks]
        return _custom_delivery_receipt(bool(sinks) and all(results))
    return _inactive_delivery_receipt()


async def flush_observability() -> DeliveryReceipt:
    """Flush events off the caller's event loop and report confirmed delivery."""
    return await asyncio.to_thread(sync_flush_observability)


def sync_shutdown_observability() -> DeliveryReceipt:
    """Shut down and return confirmed delivery, pending work, or terminal failure.

    Registered custom sinks remain owned by the application. Their storage is
    unconfirmed here; applications must flush and close those sinks themselves.
    """
    receipt = _shutdown_built_in_event_sink()
    custom_success = _shutdown_configured_named_event_sink()
    if receipt is not None:
        return receipt
    if not get_settings().observability_enabled:
        return _inactive_delivery_receipt()
    if get_settings().observability_sink_name != DEFAULT_RULE_EVENT_SINK_NAME:
        return _custom_delivery_receipt(custom_success)
    return _inactive_delivery_receipt()


async def shutdown_observability() -> DeliveryReceipt:
    """Shut down event delivery off the caller's loop and return its receipt."""
    return await asyncio.to_thread(sync_shutdown_observability)


def is_observability_enabled() -> bool:
    """Check if observability is enabled and an active sink is available."""
    return bool(_get_active_rule_event_sinks())


def log_span_start(
    trace_id: str,
    span_id: str,
    function_name: str,
    agent_name: str | None = None,
) -> None:
    """
    Log span start for debugging and correlation.

    Args:
        trace_id: Trace ID (full 32-char hex)
        span_id: Span ID (full 16-char hex)
        function_name: Name of the function being traced
        agent_name: Optional agent name
    """
    if not _should_log("span_start"):
        return

    agent_part = f" agent={agent_name}" if agent_name else ""
    logger.info(f"[trace:{trace_id}] [span:{span_id}] Span started: {function_name}{agent_part}")


def log_span_end(
    trace_id: str,
    span_id: str,
    function_name: str,
    duration_ms: float,
    executions: int = 0,
    matches: int = 0,
    non_matches: int = 0,
    errors: int = 0,
    actions: dict[str, int] | None = None,
    unresolved: int = 0,
) -> None:
    """
    Log span end with summary stats.

    Args:
        trace_id: Trace ID (full 32-char hex)
        span_id: Span ID (full 16-char hex)
        function_name: Name of the function being traced
        duration_ms: Total duration in milliseconds
        executions: Total number of rule checks
        matches: Number of rules that matched
        non_matches: Number of rules that didn't match
        errors: Number of errors during check
        actions: Dict of action counts (e.g., {"deny": 2, "observe": 1})
        unresolved: Rules without a resolved semantic decision
    """
    if not _should_log("span_end"):
        return

    actions_str = ""
    if actions:
        actions_str = " actions={" + ", ".join(f"{k}:{v}" for k, v in actions.items()) + "}"
    unresolved_str = f" unresolved={unresolved}" if unresolved else ""

    logger.info(
        f"[trace:{trace_id}] [span:{span_id}] Span completed: {function_name} "
        f"duration={duration_ms:.1f}ms exec={executions} "
        f"match={matches} non_match={non_matches} errors={errors}{unresolved_str}{actions_str}"
    )


def log_rule_checks(
    trace_id: str,
    span_id: str,
    rule_name: str,
    matched: bool,
    action: str,
    confidence: float,
    duration_ms: float | None = None,
    check_id: str | None = None,
    check_stage: str | None = None,
    outcome: str | None = None,
) -> None:
    """
    Log individual rule check for debugging.

    This allows users to search logs by check_id from the UI.

    Args:
        trace_id: Trace ID (full 32-char hex)
        span_id: Span ID (full 16-char hex)
        rule_name: Name of the rule that was evaluated
        matched: Whether the rule matched
        action: Action taken (deny, steer, observe)
        confidence: Confidence score (0.0-1.0)
        duration_ms: Check duration in milliseconds
        check_id: Unique ID for this rule check
        check_stage: Check stage (pre or post)
        outcome: Explicit semantic outcome when available
    """
    if not _should_log("rule_eval"):
        return

    parts = [
        f"[trace:{trace_id}]",
        f"[span:{span_id}]",
        f"[rule:{rule_name}]",
    ]

    if check_stage:
        parts.append(f"stage={check_stage}")

    parts.append(f"matched={matched}")
    if outcome is not None:
        parts.append(f"outcome={outcome}")

    # Only show action if the rule matched (action was actually taken)
    if matched or outcome == "unresolved":
        parts.append(f"action={action}")

    parts.append(f"confidence={confidence:.2f}")

    if duration_ms is not None:
        parts.append(f"duration={duration_ms:.1f}ms")

    if check_id:
        parts.append(f"exec_id={check_id}")

    logger.info(" ".join(parts))
