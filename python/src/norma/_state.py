# Modified for Norma.
"""
Global state management for Norma SDK.

This module holds global state in a container object to avoid circular imports
between __init__.py and other modules. Both modules can import and modify
the same state object.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, Any

from httpx import AsyncBaseTransport

from .control_contract import ControlContract

if TYPE_CHECKING:
    from norma.models import Agent

    from ._telemetry.sinks import RuleEventSink
    from .client import NormaClient
    from .observability import EventBatcher
    from .settings import SDKSettings


class _StateContainer:
    """Container for global SDK state."""

    def __init__(self) -> None:
        self.current_agent: Agent | None = None
        self.configured_agent_name: str | None = None
        self.initialization_failed = False
        self.rule_engine: Any = None
        self.client: NormaClient | None = None
        self.server_rules: list[dict[str, Any]] | None = None
        self.rule_cache_healthy = False
        self.server_url: str | None = None
        self.api_key: str | None = None
        self.api_key_header: str | None = None
        self.control_contract: ControlContract | None = None
        self.client_timeout_seconds = 30.0
        self.client_transport: AsyncBaseTransport | None = None
        # Optional target context fixed at init() time; both fields are set
        # together or both remain None.
        self.target_type: str | None = None
        self.target_id: str | None = None
        self.session_generation = 0
        self.refresh_interval_seconds = 0
        self.refresh_stop_event: Event | None = None
        self.refresh_thread: Thread | None = None
        self.observability_batcher: EventBatcher | None = None
        self.observability_sink: RuleEventSink | tuple[RuleEventSink, ...] | None = None
        self.observability_sink_owned = False
        self.observability_settings: SDKSettings | None = None
        self.observability_dropped = 0
        self.closed = False
        self.lifecycle_lock = RLock()
        self.instrumentation_slots: list[Any] = []

    def require_rule_cache(self) -> list[dict[str, Any]]:
        """Reject unknown coverage after initialization or a failed refresh."""
        self.require_agent_identity()
        if self.closed or self.server_rules is None or not self.rule_cache_healthy:
            raise RuntimeError(
                "Norma Rule cache is unavailable; execution blocked until "
                "Rules refresh successfully."
            )
        return self.server_rules

    def require_agent_identity(self) -> None:
        """Fence scoped sessions if their public mutable Agent name was changed."""
        if (
            self.configured_agent_name is not None
            and (
                self.current_agent is None
                or self.current_agent.agent_name != self.configured_agent_name
            )
        ):
            raise RuntimeError("Configured Agent identity changed; session execution blocked.")


# Singleton state instance
state = _StateContainer()

_active_state: ContextVar[_StateContainer | None] = ContextVar(
    "norma_active_session_state", default=None
)


def get_state() -> _StateContainer:
    """Return the selected configured session, or the legacy default session."""
    return _active_state.get() or state


def active_state() -> _StateContainer | None:
    """Return only a configured session selected by an instrumentation scope."""
    return _active_state.get()


@contextmanager
def use_state(selected: _StateContainer) -> Iterator[None]:
    """Select one configured session for this synchronous or async context."""
    token: Token[_StateContainer | None] = _active_state.set(selected)
    try:
        yield
    finally:
        _active_state.reset(token)
