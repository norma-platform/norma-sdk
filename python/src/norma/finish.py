# Modified for Norma.
"""Bounded SDK evidence recording and fail-open finish checks."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Literal, Self

import httpx

from norma.models import (
    ActionSummary,
    ContinuationState,
    FinishVerdict,
    Step,
    TurnRecord,
    redact_turn_record,
    redact_turn_text,
)

from ._checks import _post_checks_request
from ._state import get_state
from ._wire.checks import CheckRequest as _CheckRequest
from .client import NormaClient, new_session_client

_active_recorder: ContextVar[TurnRecorder | None] = ContextVar(
    "norma_finish_recorder", default=None
)


@dataclass
class _NativeFinishUse:
    used: bool = False


@dataclass(frozen=True)
class ConfiguredNativeFinish:
    """Invocation-local state passed to configured native finish adapters."""

    driver: str
    adapter_name: str
    agent_name: str
    recorder: TurnRecorder
    finish: Mapping[str, object]
    invocation: Mapping[str, object]
    session: object
    target_type: str | None
    target_id: str | None
    use_state: _NativeFinishUse = field(default_factory=_NativeFinishUse, compare=False)
    session_generation: int = field(init=False, repr=False)
    session_agent: object | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_generation", getattr(self.session, "session_generation", -1)
        )
        object.__setattr__(self, "session_agent", getattr(self.session, "current_agent", None))

    def ensure_active(self) -> None:
        """Fence native work after close, reinitialization or Agent identity change."""
        session = self.session
        agent = getattr(session, "current_agent", None)
        if (
            getattr(session, "closed", True)
            or getattr(session, "session_generation", None) != self.session_generation
            or agent is not self.session_agent
            or getattr(session, "configured_agent_name", None) != self.agent_name
            or agent is None
            or getattr(agent, "agent_name", None) != self.agent_name
        ):
            from .checkpoints import CheckError

            raise CheckError("Configured Agent session changed or closed; execution blocked.")


_configured_native_finish: ContextVar[ConfiguredNativeFinish | None] = ContextVar(
    "norma_configured_native_finish", default=None
)


@contextmanager
def use_configured_native_finish(record: ConfiguredNativeFinish) -> Iterator[None]:
    """Select a native finish plan only for this application invocation."""
    token = _configured_native_finish.set(record)
    try:
        yield
    finally:
        _configured_native_finish.reset(token)


def current_configured_native_finish() -> ConfiguredNativeFinish | None:
    """Return the native finish plan selected by the current configured call."""
    return _configured_native_finish.get()


def mark_configured_native_finish_used(record: ConfiguredNativeFinish) -> None:
    """Mark that the matching configured native framework call handled finish."""
    if current_configured_native_finish() is not record:
        raise RuntimeError("Configured native finish identity does not match this invocation")
    record.use_state.used = True


def configured_native_finish_was_used(record: ConfiguredNativeFinish) -> bool:
    """Report whether an eligible native framework call ran for this invocation."""
    return record.use_state.used


@contextmanager
def use_turn_recorder(recorder: TurnRecorder) -> Iterator[None]:
    """Reuse an active invocation recorder, or establish it for a native helper."""
    if _active_recorder.get() is recorder:
        yield
    else:
        with recorder:
            yield


ActionKind = Literal["shell", "edit", "read", "search", "web", "mcp", "task", "other"]
ActionStatus = Literal["succeeded", "failed", "denied", "unknown"]


class FinishHeldError(RuntimeError):
    """A held finish that preserves the original result and runtime verdict."""

    def __init__(self, verdict: FinishVerdict, original_result: object) -> None:
        self.verdict = verdict
        self.original_result = original_result
        super().__init__("The configured finish check held this turn.")


def _bounded_text(value: str, limit: int) -> str:
    """Redact before clipping, so a credential cannot straddle a clip boundary."""
    if len(value) > 65_536:
        return "[omitted oversized text]"
    return redact_turn_text(value)[:limit]


def _bounded_tail(value: str, limit: int) -> str:
    """Keep recent result evidence after redacting the entire original string."""
    if len(value) > 65_536:
        return "[omitted oversized output]"
    return redact_turn_text(value)[-limit:]


@dataclass
class TurnRecorder:
    """Collect bounded independent action evidence for one SDK turn.

    Use as a context manager around work already decorated with ``@checkpoint``.
    Calls to :meth:`record_action` cover work performed outside that decorator.
    The recorder is local to the current async context; it never reads a transcript.
    """

    session_id: str
    turn_id: str
    user_request: str | None = None
    platform: str = "python-sdk"
    platform_version: str | None = None
    model: str | None = None
    max_continuations: int = 2
    _actions: list[ActionSummary] = field(default_factory=list, init=False, repr=False)
    _files_touched: list[str] = field(default_factory=list, init=False, repr=False)
    _action_count: int = field(default=0, init=False, repr=False)
    _actions_truncated: bool = field(default=False, init=False, repr=False)
    _token: Token[TurnRecorder | None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not 0 <= self.max_continuations <= 3:
            raise ValueError("max_continuations must be between 0 and 3")
        if not self.session_id or not self.turn_id:
            raise ValueError("session_id and turn_id are required")

    def __enter__(self) -> Self:
        if self._token is not None:
            raise RuntimeError("TurnRecorder is already active")
        self._token = _active_recorder.set(self)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        assert self._token is not None
        _active_recorder.reset(self._token)
        self._token = None

    def record_action(
        self,
        *,
        kind: ActionKind,
        native_tool: str,
        status: ActionStatus,
        target: str | None = None,
        command: str | None = None,
        exit_code: int | None = None,
        output_tail: str | None = None,
        duration_ms: int | None = None,
    ) -> ActionSummary:
        """Append a normalized action; oldest evidence is evicted after 50 actions."""
        action = ActionSummary(
            index=self._action_count,
            kind=kind,
            native_tool=_bounded_text(native_tool, 256),
            status=status,
            target=_bounded_text(target, 300) if target is not None else None,
            command=_bounded_text(command, 500) if command is not None else None,
            exit_code=exit_code,
            output_tail=_bounded_tail(output_tail, 1000) if output_tail is not None else None,
            duration_ms=duration_ms,
        )
        self._action_count += 1
        self._actions.append(action)
        if len(self._actions) > 50:
            self._actions.pop(0)
            self._actions_truncated = True
        if kind == "edit" and status == "succeeded" and target is not None:
            path = _bounded_text(target, 300)
            if path not in self._files_touched:
                self._files_touched.append(path)
                if len(self._files_touched) > 200:
                    self._files_touched.pop(0)
        return action.model_copy()

    def turn(
        self,
        final_message: str,
        *,
        continuation_count: int = 0,
        agent_generation: str | None = None,
    ) -> TurnRecord:
        """Build a typed snapshot; a hold continuation keeps this turn identity."""
        return TurnRecord(
            schema_version="1",
            platform=self.platform,
            platform_version=self.platform_version,
            model=self.model,
            session_id=self.session_id,
            turn_id=self.turn_id,
            user_request=_bounded_text(self.user_request, 2000)
            if self.user_request is not None
            else None,
            final_message=_bounded_text(final_message, 8000),
            actions=[action.model_copy() for action in self._actions],
            actions_truncated=self._actions_truncated,
            files_touched=list(self._files_touched),
            evidence_source="sdk",
            continuation=ContinuationState(
                count=continuation_count,
                max=self.max_continuations,
                agent_generation=agent_generation,
            ),
        )


def _current_recorder() -> TurnRecorder | None:
    return _active_recorder.get()


def _record_decorated_action(
    name: str,
    status: ActionStatus,
    *,
    kind: ActionKind = "other",
    target: str | None = None,
    command: str | None = None,
    result: object = None,
    error: BaseException | None = None,
    duration_ms: int | None = None,
) -> None:
    """Record only facts visible at the decorated call boundary."""
    recorder = _current_recorder()
    if recorder is None:
        return
    exit_code: int | None = None
    if isinstance(result, dict):
        value = result.get("exit_code", result.get("returncode"))
        if isinstance(value, int) and not isinstance(value, bool):
            exit_code = value
        if result.get("isError") is True or result.get("status") == "failed":
            status = "failed"
    elif isinstance(result, subprocess.CompletedProcess):
        value = result.returncode
        if isinstance(value, int) and not isinstance(value, bool):
            exit_code = value
    if exit_code is not None and status == "succeeded" and exit_code != 0:
        status = "failed"
    output = result if isinstance(result, str) else None
    if error is not None:
        output = type(error).__name__
    recorder.record_action(
        kind=kind,
        native_tool=name,
        status=status,
        target=target,
        command=command,
        exit_code=exit_code,
        output_tail=output,
        duration_ms=duration_ms,
    )


def _unavailable(
    continuation: ContinuationState,
    reason: str = "Finish allowed: the rule service could not complete this check.",
) -> FinishVerdict:
    return FinishVerdict(
        verdict="unavailable",
        reason=reason,
        reason_codes=["unavailable"],
        held_by=[],
        would_hold_by=[],
        continuation=continuation,
        allow_finish=True,
        outcome="finished_unverified",
        provider_input_tokens=None,
    )


async def check_finish(
    turn: TurnRecord,
    *,
    client: NormaClient | None = None,
    agent_name: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
) -> FinishVerdict:
    """Check one proposed finish through the existing runtime authorization seam.

    The caller may supply an initialized client, agent name, and target pair,
    or use the current ``norma.init()`` session. Network, auth, and
    malformed response failures return an unavailable verdict and allow finish.
    """
    try:
        turn = redact_turn_record(turn)
    except (ValueError, TypeError, AttributeError):
        return _unavailable(ContinuationState(count=0, max=0))
    current_agent = get_state().current_agent
    resolved_agent = agent_name or (current_agent.agent_name if current_agent else None)
    from ._request_context import current_invocation
    from ._state import active_state

    invocation = current_invocation(get_state())

    try:
        get_state().require_agent_identity()
    except RuntimeError:
        return _unavailable(turn.continuation)
    if (
        client is None
        and active_state() is get_state()
        and current_agent is not None
        and resolved_agent != current_agent.agent_name
    ):
        return _unavailable(turn.continuation)
    if resolved_agent is None:
        return _unavailable(turn.continuation)
    if (target_type is None) != (target_id is None):
        return _unavailable(turn.continuation)
    if (
        client is None
        and target_type is not None
        and (
            target_type
            != (invocation.target_type if invocation is not None else get_state().target_type)
            or target_id
            != (invocation.target_id if invocation is not None else get_state().target_id)
        )
    ):
        return _unavailable(turn.continuation)
    resolved_target_type = (
        target_type
        if client is not None
        else invocation.target_type
        if invocation is not None
        else get_state().target_type
    )
    resolved_target_id = (
        target_id
        if client is not None
        else invocation.target_id
        if invocation is not None
        else get_state().target_id
    )
    try:
        request = _CheckRequest(
            agent_name=resolved_agent,
            step=Step(
                type="turn",
                name=f"{turn.platform}.finish",
                input=turn.model_dump(mode="json"),
                output=None,
                context=None,
                tools=None,
                ground_truth=None,
            ),
            stage="pre",
            target_type=resolved_target_type,
            target_id=resolved_target_id,
        )
    except (ValueError, TypeError):
        return _unavailable(turn.continuation)
    request_payload = request.model_dump(mode="json")
    if len(json.dumps(request_payload, separators=(",", ":")).encode("utf-8")) > 256 * 1024:
        return _unavailable(turn.continuation)

    async def send(active_client: NormaClient) -> FinishVerdict:
        response = await _post_checks_request(
            active_client,
            request_payload=request_payload,
            headers=None,
            target_type=request.target_type,
            target_id=request.target_id,
        )
        response.raise_for_status()
        verdict = FinishVerdict.model_validate(response.json())
        if (
            verdict.continuation.count != turn.continuation.count
            or verdict.continuation.max > turn.continuation.max
        ):
            raise ValueError("finish response has an incompatible continuation state")
        request_generation = turn.continuation.agent_generation
        response_generation = verdict.continuation.agent_generation
        if (
            response_generation is not None
            and verdict.agent_generation is not None
            and response_generation != verdict.agent_generation
        ):
            raise ValueError("finish response has inconsistent agent generations")
        if request_generation is not None and response_generation != request_generation:
            raise ValueError("finish response changed the pinned agent generation")
        return verdict

    try:
        async with asyncio.timeout(8.0):
            if client is not None:
                return await send(client)
            async with new_session_client(NormaClient) as session_client:
                return await send(session_client)
    except (httpx.HTTPError, TimeoutError, ValueError, RuntimeError):
        return _unavailable(turn.continuation)
