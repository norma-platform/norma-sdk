# Modified for Norma.
"""Norma hosted SDK: explicit agent identity, remote checks and observed execution."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("norma-sdk")
except PackageNotFoundError:
    __version__ = "0.0.0.dev"

import asyncio
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from . import agents, checks, operations
from ._checks import evaluate_rules as _evaluate_rules
from ._rule_registry import (
    StepSchemaDict,
    get_registered_steps,
    merge_explicit_and_auto_steps,
)
from ._telemetry.trace_context import clear_trace_context_provider, set_trace_context_provider
from ._wire.adapters import decision_from_wire as _decision_from_wire
from ._wire.rules import RuleDefinition as _WireRuleDefinition
from .checkpoints import (
    CheckError,
    RuleSteerError,
    RuleViolationError,
    checkpoint,
)
from .client import NormaClient
from .configuration import resolve_environment, resolve_url
from .errors import NormaAPIError
from .finish import TurnRecorder, check_finish
from .models import (
    Agent,
    Decision,
    JSONObject,
    RuleDefinition,
    RuleMatch,
    Step,
    StepSchema,
)
from .models.server import RuleRecord
from .observability import (
    DeliveryReceipt,
    add_event,
    configure_logging,
    flush_observability,
    get_logger,
    init_observability,
    shutdown_observability,
    sync_flush_observability,
    sync_shutdown_observability,
)
from .tracing import with_trace
from .validation import ensure_agent_name

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

# Module logger
logger = get_logger(__name__)


# ============================================================================
# Global State
# ============================================================================

# Import state container to avoid circular imports
from ._state import state as _session_state  # noqa: E402

F = TypeVar("F", bound=Callable[..., Any])

# Policy refresh loop state
_refresh_lock = threading.Lock()
_policy_refresh_interval_seconds: int | None = None
_refresh_thread: threading.Thread | None = None
_refresh_stop_event: threading.Event | None = None

# Session lifecycle state used to discard stale manual refresh results.
_session_lock = threading.RLock()
_session_generation = 0


@dataclass(frozen=True)
class _RefreshContext:
    """Connection/session snapshot for a single refresh request.

    The session-level target context, when set, is captured here so the
    refresh worker forwards it on each ``GET /agents/{name}/rules``
    call. The server returns the merged effective set, which is what
    ``state.server_rules`` is published from.
    """

    session_generation: int
    agent_name: str
    server_url: str
    api_key: str | None
    target_type: str | None
    target_id: str | None


def _get_server_rules() -> list[dict[str, Any]] | None:
    """Return the private runtime Rule snapshot for internal inspection."""
    return _session_state.server_rules


def _publish_server_rules(
    rules: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Publish the private Rule snapshot using swap-only semantics."""
    if rules is None:
        _session_state.server_rules = None
        _session_state.rule_cache_healthy = False
        return None

    # Snapshot copy: publish a new list reference and avoid in-place mutation.
    _session_state.server_rules = list(rules)
    _session_state.rule_cache_healthy = True
    return _session_state.server_rules


def _rules_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Distinguish an authoritative empty list from an absent/malformed cache."""
    rules = response.get("rules")
    if not isinstance(rules, list) or not all(isinstance(item, dict) for item in rules):
        raise RuntimeError("Server did not return a valid Rules list")
    for item in rules:
        if type(item.get("id")) is not int or not isinstance(item.get("name"), str):
            raise RuntimeError("Server returned a Rule without a valid identity")
        definition = _WireRuleDefinition.model_validate(item.get("rule"))
        if definition.execution == "sdk":
            raise ValueError(
                "SDK-local Rules are unsupported; migrate the Rule to hosted execution."
            )
    return rules


def _run_coro_in_new_loop[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine on a dedicated event loop in the current thread."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _ad_hoc_client(
    *,
    server_url: str,
    agent_key: str | None,
) -> NormaClient:
    return NormaClient(
        runtime_url=server_url,
        agent_key=agent_key,
    )


def _snapshot_refresh_context() -> _RefreshContext:
    """Capture a consistent session snapshot for a refresh request."""
    with _session_lock:
        session_generation = _session_generation
        agent = _session_state.current_agent
        server_url = _session_state.server_url
        api_key = _session_state.api_key
        target_type = _session_state.target_type
        target_id = _session_state.target_id

    if agent is None:
        raise RuntimeError("Agent not initialized. Call norma.init() first.")
    if server_url is None:
        raise RuntimeError("Server URL not set. Call norma.init() first.")

    return _RefreshContext(
        session_generation=session_generation,
        agent_name=agent.agent_name,
        server_url=server_url,
        api_key=api_key,
        target_type=target_type,
        target_id=target_id,
    )


async def _fetch_rules_for_context_async(context: _RefreshContext) -> list[dict[str, Any]]:
    """Fetch Rules for a previously snapshotted session context."""
    async with NormaClient(
        runtime_url=context.server_url,
        agent_key=context.api_key,
    ) as client:
        response = await agents._list_agent_rules(
            client,
            context.agent_name,
            rendered_state="rendered",
            enabled_state="enabled",
            target_type=context.target_type,
            target_id=context.target_id,
        )
        return _rules_from_response(response)


async def _fetch_rules_async() -> list[dict[str, Any]]:
    """Fetch the private runtime Rule snapshot without publishing it."""
    context = _snapshot_refresh_context()
    return await _fetch_rules_for_context_async(context)


def _policy_refresh_worker(stop_event: threading.Event, interval_seconds: int) -> None:
    """Background worker that periodically refreshes Rules."""
    while not stop_event.wait(interval_seconds):
        if stop_event.is_set():
            break
        with _session_lock:
            generation = _session_generation
        try:
            rules = _run_coro_in_new_loop(_fetch_rules_async())
        except Exception as exc:
            with _session_lock:
                if generation == _session_generation and not stop_event.is_set():
                    _session_state.rule_cache_healthy = False
            logger.error(
                "Background policy refresh loop iteration failed: %s",
                exc,
                exc_info=True,
            )
            continue
        # Do not publish if shutdown was requested during the fetch.
        if stop_event.is_set():
            break
        with _session_lock:
            if generation != _session_generation or stop_event.is_set():
                continue
            _publish_server_rules(rules)
        logger.info("Refreshed %d Rule(s) from server", len(rules))


def _stop_policy_refresh_loop() -> None:
    """Stop the background policy refresh loop if running."""
    global _policy_refresh_interval_seconds, _refresh_thread, _refresh_stop_event

    with _refresh_lock:
        stop_event = _refresh_stop_event
        refresh_thread = _refresh_thread
        _policy_refresh_interval_seconds = None
        _refresh_stop_event = None
        _refresh_thread = None

    if stop_event is not None:
        stop_event.set()

    if refresh_thread is not None and refresh_thread.is_alive():
        refresh_thread.join(timeout=2)
        if refresh_thread.is_alive():
            logger.warning("Timed out while stopping policy refresh loop thread")


def _start_policy_refresh_loop(interval_seconds: int) -> None:
    """Start a background loop that refreshes Rules on a fixed interval."""
    global _policy_refresh_interval_seconds, _refresh_thread, _refresh_stop_event

    if interval_seconds <= 0:
        return

    stop_event = threading.Event()
    refresh_thread = threading.Thread(
        target=_policy_refresh_worker,
        args=(stop_event, interval_seconds),
        name="norma-policy-refresh",
        daemon=True,
    )
    with _refresh_lock:
        _policy_refresh_interval_seconds = interval_seconds
        _refresh_stop_event = stop_event
        _refresh_thread = refresh_thread

    refresh_thread.start()
    logger.info("Started policy refresh loop (interval=%ss)", interval_seconds)


async def _refresh_rules_async() -> list[dict[str, Any]] | None:
    """Refresh the private Rule snapshot; failed refreshes block session checks."""
    context = _snapshot_refresh_context()
    try:
        rules = await _fetch_rules_for_context_async(context)
    except Exception as exc:
        with _session_lock:
            if context.session_generation == _session_generation:
                _session_state.rule_cache_healthy = False
        logger.error(
            "Failed to refresh Rules; retaining previous cache for inspection "
            "but blocking session checks (%d Rule(s)): %s",
            len(_session_state.server_rules or []),
            exc,
            exc_info=True,
        )
        return _session_state.server_rules

    with _session_lock:
        if context.session_generation != _session_generation:
            logger.info("Discarding stale Rule refresh result for superseded SDK session")
            return _session_state.server_rules

        refreshed_rules = _publish_server_rules(rules)

    if refreshed_rules is not None:
        logger.info("Refreshed %d Rule(s) from server", len(refreshed_rules or []))
    else:
        logger.info("Refreshed 0 Rule(s) from server")
    return refreshed_rules


def _refresh_rules() -> list[dict[str, Any]] | None:
    """Refresh the private Rule snapshot synchronously, preserving lifecycle checks."""
    try:
        asyncio.get_running_loop()
        # We're in an async context - run in thread
        result_container: list[list[dict[str, Any]] | None] = [None]
        exception_container: list[Exception | None] = [None]

        def run_in_thread() -> None:
            try:
                result_container[0] = _run_coro_in_new_loop(_refresh_rules_async())
            except Exception as e:
                exception_container[0] = e

        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
        thread.join(timeout=10)

        if exception_container[0]:
            raise exception_container[0]
        return result_container[0]

    except RuntimeError:
        # No running event loop - we're in a sync context
        return _run_coro_in_new_loop(_refresh_rules_async())


# ============================================================================
# Public API Functions
# ============================================================================


def init(
    agent_name: str | None = None,
    agent_description: str | None = None,
    agent_version: str | None = None,
    runtime_url: str | None = None,
    agent_key: str | None = None,
    steps: list[StepSchemaDict] | None = None,
    conflict_mode: Literal["strict", "overwrite"] = "strict",
    observability_enabled: bool | None = None,
    observability_sink_name: str | None = None,
    observability_sink_config: JSONObject | None = None,
    log_config: dict[str, Any] | None = None,
    policy_refresh_interval_seconds: int = 60,
    target_type: str | None = None,
    target_id: str | None = None,
    otel_tracer_provider: TracerProvider | None = None,
    **kwargs: object,
) -> Agent:
    """Initialize a ready Norma session or raise without publishing partial state.

    Define checkpoints first. Create the agent and issue its scoped Agent Key in
    Console, then call this function. Strict mode checks the saved checkpoint
    schemas; only an authorized owner can replace them. After any startup error,
    fix the cause and initialize successfully before running the agent.

    Pass agent_name explicitly. runtime_url and agent_key default to
    NORMA_RUNTIME_URL and NORMA_AGENT_KEY. Target context is fixed for this
    session. Rules are hosted; manage Rules in
    Console. Set policy_refresh_interval_seconds=0 to disable cache refresh.

    Returns:
        The initialized Agent, after registration and Rule configuration validation.

    Raises:
        NormaAPIError: The API rejected initialization (also an HTTPStatusError).
        CheckError: The returned Rule configuration is invalid.
        httpx.RequestError: The server cannot be reached.
        TimeoutError: Initialization did not finish within 30 seconds.
        ValueError: Configuration is invalid.
    """
    if "rules_file" in kwargs:
        raise TypeError("Local Rule files are unsupported; manage hosted Rules in Console.")
    _stop_policy_refresh_loop()
    with _session_lock:
        _reset_state()
        _session_state.initialization_failed = True
        initialization_generation = _session_generation
    resolved_agent_name = agent_name
    if not resolved_agent_name:
        raise ValueError(
            "The 'agent_name' argument is required for initialization. "
            "Set the exact agent name from its Connection page in Console."
        )
    normalized_name = ensure_agent_name(resolved_agent_name)
    if conflict_mode not in ("strict", "overwrite"):
        raise ValueError("conflict_mode must be 'strict' or 'overwrite'.")
    if policy_refresh_interval_seconds < 0:
        raise ValueError("policy_refresh_interval_seconds must be >= 0")
    if (target_type is None) != (target_id is None):
        raise ValueError("target_type and target_id must be supplied together.")
    resolved_url = resolve_url(runtime_url)
    resolved_key = resolve_environment("AGENT_KEY", agent_key)
    resolved_header = "X-API-Key"
    NormaClient(runtime_url=resolved_url, agent_key=resolved_key)
    next_agent = Agent(
        agent_name=normalized_name,
        agent_description=agent_description,
        agent_created_at=datetime.now(UTC).isoformat(),
        agent_updated_at=None,
        agent_version=agent_version,
        agent_metadata=kwargs,
    )
    merged = merge_explicit_and_auto_steps(steps, get_registered_steps())
    registration_steps: list[dict[str, Any]] = [dict(step) for step in merged.steps]
    if merged.overridden_keys:
        logger.warning(
            "Skipping %d auto-discovered step(s) overridden by explicit steps",
            len(merged.overridden_keys),
        )
    # Validate the complete proposed schema before making any request.
    for step in registration_steps:
        StepSchema.model_validate(step)

    if log_config:
        configure_logging(log_config)

    async def register() -> list[dict[str, Any]]:
        async with NormaClient(
            runtime_url=resolved_url,
            agent_key=resolved_key,
        ) as client:
            response = await agents._register_agent(
                client,
                next_agent,
                steps=registration_steps,
                conflict_mode=conflict_mode,
                target_type=target_type,
                target_id=target_id,
            )
            try:
                loaded_rules = _rules_from_response(response)
                logger.info("Agent initialized: %s", normalized_name)
                return loaded_rules
            except (RuntimeError, ValueError, TypeError, KeyError):
                raise CheckError(
                    "Norma returned invalid Rule configuration. Initialization failed; "
                    "ask a workspace owner to review the agent's Rules in Console."
                ) from None

    async def register_with_timeout() -> list[dict[str, Any]]:
        return await asyncio.wait_for(register(), timeout=30)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_loop = False
    else:
        running_loop = True

    try:
        if running_loop:
            results: list[list[dict[str, Any]]] = []
            errors: list[BaseException] = []

            def run_in_thread() -> None:
                try:
                    results.append(_run_coro_in_new_loop(register_with_timeout()))
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run_in_thread, daemon=True)
            thread.start()
            thread.join(timeout=31)
            if thread.is_alive():
                raise TimeoutError("Norma initialization timed out; execution remains blocked.")
            if errors:
                raise errors[0]
            server_rules = results[0]
        else:
            server_rules = _run_coro_in_new_loop(register_with_timeout())

        # A newer initialization or shutdown supersedes this attempt. Check while
        # holding the lifecycle lock before installing telemetry or publishing state.
        with _session_lock:
            if initialization_generation != _session_generation:
                raise CheckError(
                    "Norma initialization was superseded by a newer session or shutdown."
                )
            init_observability(
                runtime_url=resolved_url,
                agent_key=resolved_key,
                enabled=observability_enabled,
                sink_name=observability_sink_name,
                sink_config=observability_sink_config,
                otel_tracer_provider=otel_tracer_provider,
                new_session=True,
            )
            _session_state.current_agent = next_agent
            _session_state.server_rules = server_rules
            _session_state.rule_cache_healthy = True
            _session_state.server_url = resolved_url
            _session_state.api_key = resolved_key
            _session_state.api_key_header = resolved_header
            _session_state.target_type = target_type
            _session_state.target_id = target_id
            _session_state.initialization_failed = False
            if policy_refresh_interval_seconds > 0:
                _start_policy_refresh_loop(policy_refresh_interval_seconds)
    except BaseException:
        with _session_lock:
            if initialization_generation == _session_generation:
                _reset_state()
                _session_state.initialization_failed = True
        raise
    return next_agent


def checkpoint_schemas() -> list[StepSchema]:
    """Return registered checkpoint schemas for Console's schema update review.

    Define/import checkpoints before calling this helper. Serialize with
    ``[schema.model_dump(mode="json", exclude_none=True) for schema in checkpoint_schemas()]``.
    Explicit schemas passed separately to ``init(steps=...)`` must also be included
    in the owner's proposed schema update.
    """
    return [StepSchema.model_validate(step) for step in get_registered_steps()]


# ============================================================================
# Shutdown
# ============================================================================


def _reset_state() -> None:
    """Clear all global SDK state."""
    global _session_generation

    with _session_lock:
        _session_generation += 1
        _session_state.current_agent = None
        _session_state.initialization_failed = False
        _session_state.rule_engine = None
        _session_state.server_rules = None
        _session_state.rule_cache_healthy = False
        _session_state.server_url = None
        _session_state.api_key = None
        _session_state.api_key_header = None
        _session_state.target_type = None
        _session_state.target_id = None


async def ashutdown() -> DeliveryReceipt:
    """Shut down the SDK and release all background resources.

    Flushes pending observability events, stops the policy refresh loop,
    and resets global state.  Idempotent - safe to call multiple times.

    Use this from async code.  For sync code use :func:`shutdown`.
    """
    await asyncio.to_thread(_stop_policy_refresh_loop)
    receipt = await shutdown_observability()
    _reset_state()
    logger.info("Norma SDK shut down")
    return receipt


def shutdown() -> DeliveryReceipt:
    """Shut down the SDK and release all background resources.

    Flushes pending observability events, stops the policy refresh loop,
    and resets global state.  Idempotent - safe to call multiple times.

    Use this from sync code.  For async code use :func:`ashutdown`.
    """
    _stop_policy_refresh_loop()
    receipt = sync_shutdown_observability()
    _reset_state()
    logger.info("Norma SDK shut down")
    return receipt


def flush() -> DeliveryReceipt:
    """Flush pending observations and return their acknowledged delivery status."""
    return sync_flush_observability()


async def aflush() -> DeliveryReceipt:
    """Flush pending observations from async code and return a delivery receipt."""
    return await flush_observability()


async def get_agent(
    agent_name: str,
    runtime_url: str | None = None,
    agent_key: str | None = None,
) -> dict[str, Any]:
    """
    Get agent details from the server by name.

    Args:
        agent_name: Agent identifier
        runtime_url: Optional tenant runtime URL (defaults to NORMA_RUNTIME_URL)
        agent_key: Scoped Agent Key (defaults to NORMA_AGENT_KEY)

    Returns:
        Dictionary containing:
            - agent: Agent metadata
            - steps: List of steps registered with the agent

    Raises:
        httpx.HTTPError: If request fails or agent not found

    Example:
        import asyncio
        import norma

        # Fetch agent from server
        async def main():
            agent_data = await norma.get_agent(
                "customer-service-bot"
            )
            print(f"Agent: {agent_data['agent']['agent_name']}")
            print(f"Steps: {len(agent_data['steps'])}")

        asyncio.run(main())

        # Or using the client directly
        async with norma.NormaClient() as client:
            agent_data = await norma.agents.get_agent(
                client, "customer-service-bot"
            )
    """
    _final_server_url = resolve_url(runtime_url)

    async with _ad_hoc_client(
        server_url=_final_server_url,
        agent_key=agent_key,
    ) as client:
        return await agents.get_agent(client, agent_name)


def current_agent() -> Agent | None:
    """
    Get the currently initialized agent (from init()).

    Returns:
        Current Agent instance or None if not initialized

    Example:
        norma.init(
            agent_name="My Bot",
        )
        agent = norma.current_agent()
        print(agent.agent_name)  # "My Bot"
    """
    return _session_state.current_agent


async def list_agents(
    runtime_url: str | None = None,
    agent_key: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """
    List all registered agents from the server.

    Args:
        runtime_url: Optional tenant runtime URL (defaults to NORMA_RUNTIME_URL)
        agent_key: Scoped Agent Key (defaults to NORMA_AGENT_KEY)
        cursor: Optional cursor for pagination (agent name of last item from previous page)
        limit: Number of results per page (default 20, max 100)

    Returns:
        Dictionary containing:
            - agents: List of agent summaries with agent_name,
                      policy_id, created_at, step_count, evaluator_count
            - pagination: Object with limit, total, next_cursor, has_more

    Raises:
        httpx.HTTPError: If request fails

    Example:
        import asyncio
        import norma

        async def main():
            result = await norma.list_agents()
            print(f"Total agents: {result['pagination']['total']}")
            for agent in result['agents']:
                print(f"  - {agent['agent_name']}")
            # Fetch next page
            if result['pagination']['has_more']:
                next_page = await norma.list_agents(
                    cursor=result['pagination']['next_cursor']
                )

        asyncio.run(main())
    """
    _final_server_url = resolve_url(runtime_url)

    async with _ad_hoc_client(
        server_url=_final_server_url,
        agent_key=agent_key,
    ) as client:
        return await agents.list_agents(client, cursor=cursor, limit=limit)


# ============================================================================
# Agent Policy/Rule Convenience Functions
# ============================================================================


__all__ = [
    "evaluate_rules",
    "Decision",
    "RuleDefinition",
    "RuleMatch",
    "add_event",
    "init_observability",
    "sync_shutdown_observability",
    "Agent",
    "Step",
    "StepSchema",
    "NormaClient",
    "NormaAPIError",
    "CheckError",
    "RuleViolationError",
    "RuleSteerError",
    "checkpoint",
    "checkpoint_schemas",
    "init",
    "shutdown",
    "ashutdown",
    "flush",
    "aflush",
    "current_agent",
    "get_agent",
    "list_agents",
    "refresh_rules",
    "refresh_rules_async",
    "checks",
    "agents",
    "operations",
    "TurnRecorder",
    "check_finish",
    "DeliveryReceipt",
    "with_trace",
    "set_trace_context_provider",
    "clear_trace_context_provider",
]


async def evaluate_rules(
    step_name: str,
    *,
    agent_name: str,
    input: Any | None = None,
    output: Any | None = None,
    context: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    ground_truth: Any | None = None,
    step_type: str = "llm",
    stage: Literal["pre", "post"] = "pre",
    target_type: str | None = None,
    target_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> Decision:
    """Check a proposal in the initialized hosted session; inspect its Decision."""
    if _session_state.current_agent is None:
        raise CheckError("Norma is not initialized; Rule check blocked.")
    response = await _evaluate_rules(
        step_name,
        agent_name=agent_name,
        input=input,
        output=output,
        context=context,
        tools=tools,
        ground_truth=ground_truth,
        step_type=step_type,
        stage=stage,
        target_type=target_type,
        target_id=target_id,
        trace_id=trace_id,
        span_id=span_id,
    )
    return _decision_from_wire(response)


async def refresh_rules_async() -> list[RuleRecord] | None:
    """Refresh hosted Rules; failed refreshes block subsequent session checks."""
    data = await _refresh_rules_async()
    return agents._public_rules({"rules": data}).rules if data is not None else None


def refresh_rules() -> list[RuleRecord] | None:
    """Refresh hosted Rules synchronously; failed refreshes block checks."""
    data = _refresh_rules()
    return agents._public_rules({"rules": data}).rules if data is not None else None
