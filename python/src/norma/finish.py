# Modified for Norma.
"""Bounded SDK evidence recording and fail-open finish checks."""

from __future__ import annotations

import asyncio
import json
import subprocess
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
from ._state import state as _session_state
from ._wire.checks import CheckRequest as _CheckRequest
from .client import NormaClient

_active_recorder: ContextVar[TurnRecorder | None] = ContextVar(
    "norma_finish_recorder", default=None
)
ActionKind = Literal["shell", "edit", "read", "search", "web", "mcp", "task", "other"]
ActionStatus = Literal["succeeded", "failed", "denied", "unknown"]


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

    def turn(self, final_message: str, *, continuation_count: int = 0) -> TurnRecord:
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
            continuation=ContinuationState(count=continuation_count, max=self.max_continuations),
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
    resolved_agent = agent_name or (
        _session_state.current_agent.agent_name if _session_state.current_agent else None
    )
    if resolved_agent is None:
        return _unavailable(turn.continuation)
    if (target_type is None) != (target_id is None):
        return _unavailable(turn.continuation)
    if (
        client is None
        and target_type is not None
        and (target_type != _session_state.target_type or target_id != _session_state.target_id)
    ):
        return _unavailable(turn.continuation)
    resolved_target_type = target_type if client is not None else _session_state.target_type
    resolved_target_id = target_id if client is not None else _session_state.target_id
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
        return verdict

    try:
        async with asyncio.timeout(8.0):
            if client is not None:
                return await send(client)
            async with NormaClient(
                runtime_url=_session_state.server_url,
                agent_key=_session_state.api_key,
            ) as session_client:
                return await send(session_client)
    except (httpx.HTTPError, TimeoutError, ValueError, RuntimeError):
        return _unavailable(turn.continuation)
