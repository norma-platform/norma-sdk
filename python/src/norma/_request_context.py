# Modified for Norma.
"""Context carried by one configured entrypoint invocation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from ._state import _StateContainer


@dataclass(frozen=True)
class _InvocationContext:
    session: _StateContainer
    target_type: str | None
    target_id: str | None
    context: dict[str, Any]
    trace_ids: tuple[str, str] | None = None


_active_invocation: ContextVar[_InvocationContext | None] = ContextVar(
    "norma_active_invocation_context", default=None
)


def current_invocation(session: _StateContainer | None = None) -> _InvocationContext | None:
    """Return the current context only for its exact owning configured session."""
    selected = _active_invocation.get()
    if selected is None or (session is not None and selected.session is not session):
        return None
    return selected


def set_invocation(
    session: _StateContainer,
    target_type: str | None,
    target_id: str | None,
    context: dict[str, Any],
    trace_ids: tuple[str, str] | None = None,
) -> Token[_InvocationContext | None]:
    return _active_invocation.set(
        _InvocationContext(session, target_type, target_id, context, trace_ids)
    )


def reset_invocation(token: Token[_InvocationContext | None]) -> None:
    _active_invocation.reset(token)


@contextmanager
def use_invocation(
    session: _StateContainer,
    target_type: str | None,
    target_id: str | None,
    context: dict[str, Any],
    trace_ids: tuple[str, str] | None = None,
) -> Iterator[None]:
    token = set_invocation(session, target_type, target_id, context, trace_ids)
    try:
        yield
    finally:
        reset_invocation(token)
