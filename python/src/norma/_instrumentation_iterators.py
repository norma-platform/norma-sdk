# Modified for Norma.
"""Iterator proxies that scope each advancement of configured entrypoints."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import (
    AsyncGenerator,
    Callable,
    Generator,
)
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from threading import Lock
from typing import Any, TypeVar, cast

_Yield = TypeVar("_Yield")
_Send = TypeVar("_Send")
_Return = TypeVar("_Return")

ScopeFactory = Callable[
    [bool], AbstractContextManager[None] | AbstractAsyncContextManager[None]
]
LiveCheck = Callable[[], None]
YieldCallback = Callable[[_Yield], object]
FinishCallback = Callable[[object], object]


class _ScopedGenerator(Generator[_Yield, _Send, _Return]):
    def __init__(
        self,
        iterator: Generator[_Yield, _Send, _Return],
        scope_factory: ScopeFactory,
        *,
        ensure_live: LiveCheck | None = None,
        on_yield: YieldCallback[_Yield] | None = None,
        on_finish: FinishCallback | None = None,
    ) -> None:
        self._iterator = iterator
        self._scope_factory = scope_factory
        self._ensure_live = ensure_live
        self._on_yield = on_yield
        self._on_finish = on_finish
        self._terminal = False
        self._advancing = Lock()

    def __iter__(self) -> _ScopedGenerator[_Yield, _Send, _Return]:
        return self

    def __next__(self) -> _Yield:
        return self._advance(lambda: next(self._iterator))

    def send(self, value: _Send) -> _Yield:
        return self._advance(lambda: self._iterator.send(value))

    def throw(self, *args: Any) -> _Yield:
        return self._advance(lambda: self._iterator.throw(*args))

    def close(self) -> Any:
        if not self._advancing.acquire(blocking=False):
            raise ValueError("generator already executing")
        self._terminal = True
        try:
            with cast(AbstractContextManager[None], self._scope_factory(True)):
                return self._iterator.close()
        finally:
            self._advancing.release()

    def _advance(self, advance: Callable[[], _Yield]) -> _Yield:
        if not self._advancing.acquire(blocking=False):
            raise ValueError("generator already executing")
        try:
            with cast(AbstractContextManager[None], self._scope_factory(False)):
                try:
                    value = advance()
                except StopIteration as stopped:
                    if self._terminal:
                        raise
                    self._terminal = True
                    if self._ensure_live is not None:
                        self._ensure_live()
                    if self._on_finish is not None:
                        self._on_finish(stopped.value)
                    raise
                except BaseException:
                    if getattr(self._iterator, "gi_frame", None) is None:
                        self._terminal = True
                    raise
                try:
                    if self._ensure_live is not None:
                        self._ensure_live()
                    if self._on_yield is not None:
                        self._on_yield(value)
                    if self._ensure_live is not None:
                        self._ensure_live()
                except BaseException:
                    self._terminal = True
                    try:
                        self._iterator.close()
                    except BaseException:
                        pass
                    raise
                return value
        finally:
            self._advancing.release()


def wrap_sync_iterator(  # noqa: UP047 -- SDK supports Python versions before PEP 695.
    iterator: Generator[_Yield, _Send, _Return],
    scope_factory: ScopeFactory,
    *,
    ensure_live: LiveCheck | None = None,
    on_yield: YieldCallback[_Yield] | None = None,
    on_finish: FinishCallback | None = None,
) -> Generator[_Yield, _Send, _Return]:
    """Scope each protocol call and optionally observe yielded/natural-finish values."""
    return _ScopedGenerator(
        iterator,
        scope_factory,
        ensure_live=ensure_live,
        on_yield=on_yield,
        on_finish=on_finish,
    )


class _ScopedAsyncGenerator(AsyncGenerator[_Yield, _Send]):
    def __init__(
        self,
        iterator: AsyncGenerator[_Yield, _Send],
        scope_factory: ScopeFactory,
        *,
        ensure_live: LiveCheck | None = None,
        on_yield: YieldCallback[_Yield] | None = None,
        on_finish: FinishCallback | None = None,
    ) -> None:
        self._iterator = iterator
        self._scope_factory = scope_factory
        self._ensure_live = ensure_live
        self._on_yield = on_yield
        self._on_finish = on_finish
        self._terminal = False
        self._finish_aborted = False
        self._advancing = False

    def __aiter__(self) -> _ScopedAsyncGenerator[_Yield, _Send]:
        return self

    async def __anext__(self) -> _Yield:
        return await self._advance_protocol(self._iterator.__anext__, "anext")

    async def asend(self, value: _Send) -> _Yield:
        return await self._advance_protocol(lambda: self._iterator.asend(value), "asend")

    async def athrow(self, *args: Any) -> _Yield:
        return await self._advance_protocol(lambda: self._iterator.athrow(*args), "athrow")

    async def aclose(self) -> None:
        if self._advancing:
            raise RuntimeError("async generator is already running")
        self._terminal = True
        scope = self._scope_factory(True)
        if hasattr(scope, "__aenter__"):
            async with cast(AbstractAsyncContextManager[None], scope):
                await self._iterator.aclose()
        else:
            with cast(AbstractContextManager[None], scope):
                await self._iterator.aclose()

    async def _advance_protocol(self, factory: Callable[[], Any], operation: str) -> _Yield:
        if self._advancing:
            raise RuntimeError(f"{operation}(): asynchronous generator is already running")
        self._observe_cancellation()
        self._advancing = True
        try:
            return await self._advance(factory())
        finally:
            self._observe_cancellation()
            self._advancing = False

    def _observe_cancellation(self) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            self._finish_aborted = True

    async def _advance(self, advancement: Any) -> _Yield:
        scope = self._scope_factory(False)
        if hasattr(scope, "__aenter__"):
            async with cast(AbstractAsyncContextManager[None], scope):
                return await self._advance_in_scope(advancement)
        with cast(AbstractContextManager[None], scope):
            return await self._advance_in_scope(advancement)

    async def _advance_in_scope(self, advancement: Any) -> _Yield:
        try:
            value = await advancement
        except StopAsyncIteration:
            if self._terminal:
                raise
            self._terminal = True
            self._observe_cancellation()
            if self._ensure_live is not None:
                self._ensure_live()
            if self._on_finish is not None and not self._finish_aborted:
                result = self._on_finish(None)
                if inspect.isawaitable(result):
                    await result
            raise
        except BaseException:
            self._observe_cancellation()
            if getattr(self._iterator, "ag_frame", None) is None:
                self._terminal = True
            raise
        try:
            self._observe_cancellation()
            if self._ensure_live is not None:
                self._ensure_live()
            if self._on_yield is not None and not self._finish_aborted:
                result = self._on_yield(value)
                if inspect.isawaitable(result):
                    await result
            self._observe_cancellation()
            if self._ensure_live is not None:
                self._ensure_live()
        except BaseException:
            self._terminal = True
            try:
                await asyncio.shield(self._iterator.aclose())
            except BaseException:
                pass
            raise
        return value


def wrap_async_iterator(  # noqa: UP047 -- SDK supports Python versions before PEP 695.
    iterator: AsyncGenerator[_Yield, _Send],
    scope_factory: ScopeFactory,
    *,
    ensure_live: LiveCheck | None = None,
    on_yield: YieldCallback[_Yield] | None = None,
    on_finish: FinishCallback | None = None,
) -> AsyncGenerator[_Yield, _Send]:
    """Scope each async protocol call and observe yields/natural finish when configured."""
    return _ScopedAsyncGenerator(
        iterator,
        scope_factory,
        ensure_live=ensure_live,
        on_yield=on_yield,
        on_finish=on_finish,
    )
