# Modified for Norma.
"""Shared sink-selection models and registries for Rule-event delivery."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TypeVar

from pydantic import BaseModel, Field

from norma.models import JSONObject

DEFAULT_RULE_EVENT_SINK_NAME = "default"
REGISTERED_RULE_EVENT_SINK_NAME = "registered"

SinkT = TypeVar("SinkT")
type RuleEventSinkFactory[SinkT] = Callable[[JSONObject], SinkT]


class SinkSelectionError(LookupError):
    """Raised when a configured sink cannot be resolved."""


class RuleEventSinkSelection(BaseModel):
    """Runtime-agnostic sink-selection payload.

    This model intentionally shares only the selection shape across runtimes.
    The meaning of built-in sink names such as ``default`` or ``registered``,
    along with any supported config keys, is defined by the caller/runtime
    that resolves the selection.
    """

    name: str = Field(
        default=DEFAULT_RULE_EVENT_SINK_NAME,
        min_length=1,
        description=(
            "Sink selection name. Built-in names are interpreted by the runtime "
            "that resolves this selection."
        ),
    )
    config: JSONObject = Field(
        default_factory=dict,
        description=(
            "Runtime-specific configuration for the selected sink. Shared only "
            "as a transport shape, not as cross-runtime semantics."
        ),
    )


class RuleEventSinkFactoryRegistry[SinkT]:
    """Thread-safe registry for named Rule-event sink factories."""

    def __init__(self) -> None:
        self._factories: dict[str, RuleEventSinkFactory[SinkT]] = {}
        self._lock = Lock()

    def register(self, name: str, factory: RuleEventSinkFactory[SinkT]) -> None:
        """Register a named sink factory."""
        if name in {
            DEFAULT_RULE_EVENT_SINK_NAME,
            REGISTERED_RULE_EVENT_SINK_NAME,
        }:
            raise ValueError(f"'{name}' is reserved and cannot be registered as a custom sink")

        with self._lock:
            self._factories[name] = factory

    def unregister(self, name: str) -> None:
        """Unregister a previously registered named sink factory."""
        with self._lock:
            self._factories.pop(name, None)

    def registered_names(self) -> tuple[str, ...]:
        """Return the registered sink names."""
        with self._lock:
            return tuple(sorted(self._factories))

    def resolve(self, selection: RuleEventSinkSelection) -> SinkT:
        """Resolve a configured named sink."""
        if selection.name in {
            DEFAULT_RULE_EVENT_SINK_NAME,
            REGISTERED_RULE_EVENT_SINK_NAME,
        }:
            raise SinkSelectionError(
                f"'{selection.name}' must be resolved by the caller as a built-in sink"
            )

        with self._lock:
            factory = self._factories.get(selection.name)

        if factory is None:
            raise SinkSelectionError(f"No Rule-event sink registered for '{selection.name}'")

        return factory(dict(selection.config))
