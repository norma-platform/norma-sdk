# Modified for Norma.
"""Bounded, JSON-only snapshots supplied by application code at a checkpoint."""

import inspect
from collections.abc import Awaitable, Callable
from typing import cast

from _norma_config.observed_state import (
    MAX_STATE_BYTES,
    MAX_STATE_DEPTH,
    MAX_STATE_NODES,
)
from _norma_config.observed_state import (
    copy_observed_state as _copy_observed_state,
)
from norma.models import JSONObject

__all__ = [
    "MAX_STATE_BYTES",
    "MAX_STATE_DEPTH",
    "MAX_STATE_NODES",
    "StateSource",
    "copy_observed_state",
    "resolve_observed_state",
]

type StateSource = JSONObject | Callable[..., JSONObject | Awaitable[JSONObject]]


def copy_observed_state(value: object) -> JSONObject:
    """Validate and detach a small plain JSON object without object serialization hooks."""
    return cast(JSONObject, _copy_observed_state(value))


async def resolve_observed_state(
    source: StateSource, args: tuple[object, ...], kwargs: dict[str, object]
) -> JSONObject:
    """Resolve once per invocation and retain only a detached JSON snapshot."""
    value = source(*args, **kwargs) if callable(source) else source
    if inspect.isawaitable(value):
        value = await value
    return copy_observed_state(value)
