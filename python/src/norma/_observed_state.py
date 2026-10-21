# Modified for Norma.
"""Bounded, JSON-only snapshots supplied by application code at a checkpoint."""

import inspect
import json
import math
from collections.abc import Awaitable, Callable
from typing import cast

from norma.models import JSONObject

# Match the TypeScript SDK and runtime limits. These limits include object keys.
MAX_STATE_BYTES = 65_536
MAX_STATE_DEPTH = 32
MAX_STATE_NODES = 10_000

type StateSource = JSONObject | Callable[..., JSONObject | Awaitable[JSONObject]]


def copy_observed_state(value: object) -> JSONObject:
    """Validate and detach a small plain JSON object without object serialization hooks."""
    remaining = MAX_STATE_NODES
    ancestors: set[int] = set()

    def validate(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_STATE_DEPTH:
            raise ValueError("Checkpoint state exceeds its nesting or item limit")
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is str:
            if len(cast(str, item)) > MAX_STATE_BYTES:
                raise ValueError("Checkpoint state exceeds 65536 UTF-8 bytes")
            return
        if kind is int:
            if abs(cast(int, item)) > 2**53 - 1:
                raise ValueError(
                    "Checkpoint state integers must be safely representable in JSON clients"
                )
            return
        if kind is float:
            number = cast(float, item)
            if not math.isfinite(number):
                raise ValueError("Checkpoint state requires finite JSON numbers")
            if number == 0 and math.copysign(1, number) < 0:
                raise ValueError("Checkpoint state does not support negative zero")
            if number.is_integer() and abs(number) > 2**53 - 1:
                raise ValueError(
                    "Checkpoint state integers must be safely representable in JSON clients"
                )
            return
        if kind not in (dict, list):
            raise ValueError("Checkpoint state accepts only plain JSON values")
        identity = id(item)
        if identity in ancestors:
            raise ValueError("Checkpoint state cannot contain cycles")
        ancestors.add(identity)
        try:
            if kind is dict:
                for key, child in cast(dict[object, object], item).items():
                    if type(key) is not str:
                        raise ValueError("Checkpoint state keys must be strings")
                    validate(key, depth + 1)
                    validate(child, depth + 1)
            else:
                for child in cast(list[object], item):
                    validate(child, depth + 1)
        finally:
            ancestors.remove(identity)

    if type(value) is not dict:
        raise ValueError("Checkpoint state must be a plain JSON object")
    validate(value, 0)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("Checkpoint state exceeds 65536 UTF-8 bytes")
    return cast(JSONObject, json.loads(encoded))


async def resolve_observed_state(
    source: StateSource, args: tuple[object, ...], kwargs: dict[str, object]
) -> JSONObject:
    """Resolve once per invocation and retain only a detached JSON snapshot."""
    value = source(*args, **kwargs) if callable(source) else source
    if inspect.isawaitable(value):
        value = await value
    return copy_observed_state(value)
