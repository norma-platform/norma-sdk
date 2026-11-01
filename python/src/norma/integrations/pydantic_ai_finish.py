# Modified for Norma.
"""Pydantic AI output-validator integration for Norma finish rules.

Install with ``norma-sdk[pydantic-ai]``. A reusable guard registers
exactly one validator per agent. Context-local run state prevents concurrent
turns from sharing evidence or continuation counts. Native ``ModelRetry``
retains completed tool history within the same agent run.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from weakref import WeakValueDictionary

from pydantic_ai import Agent, AgentRunResult, ModelRetry, RunContext

from norma.client import NormaClient
from norma.finish import TurnRecorder, _unavailable, check_finish
from norma.models import FinishVerdict, TurnRecord


@dataclass(frozen=True)
class PydanticFinishResult:
    """A completed framework run and its final Norma finish verdict."""

    result: AgentRunResult[str]
    verdict: FinishVerdict
    turn: TurnRecord


@dataclass
class _RunState:
    recorder: TurnRecorder
    client: NormaClient | None
    agent_name: str | None
    target_type: str | None
    target_id: str | None
    ensure_active: Callable[[], None] | None = None
    count: int = 0
    agent_generation: str | None = None
    verdict: FinishVerdict | None = None
    turn: TurnRecord | None = None


_guards: WeakValueDictionary[int, PydanticFinishGuard] = WeakValueDictionary()


class PydanticFinishGuard:
    """Register one context-local text-output validator on a Pydantic AI agent."""

    def __new__(
        cls, agent: Agent[Any, str], *, _defer_install: bool = False
    ) -> PydanticFinishGuard:
        _ = _defer_install
        existing = _guards.get(id(agent))
        if existing is not None and existing._agent_ref() is agent:
            return existing
        guard = super().__new__(cls)
        _guards[id(agent)] = guard
        return guard

    def __init__(self, agent: Agent[Any, str], *, _defer_install: bool = False) -> None:
        if not hasattr(self, "_run_state"):
            self._agent_ref = weakref.ref(agent)
            self._run_state: ContextVar[_RunState | None] = ContextVar(
                "norma_pydantic_finish_run", default=None
            )
            self._validator_entry: object | None = None
        if not _defer_install:
            self.install()

    def install(self) -> bool:
        """Install this guard once; return whether this call added its validator."""
        agent = self._agent_ref()
        if agent is None:
            raise RuntimeError("Pydantic AI agent no longer exists")
        validators = getattr(agent, "_output_validators", None)
        if not isinstance(validators, list):
            raise RuntimeError("Pydantic AI Agent output validator storage is unavailable")
        if self._validator_entry is not None and any(
            entry is self._validator_entry for entry in validators
        ):
            return False
        self._validator_entry = None
        before = len(validators)
        agent.output_validator(self._validate)
        if len(validators) != before + 1:
            raise RuntimeError("Pydantic AI did not register the Norma finish validator")
        self._validator_entry = validators[-1]
        return True

    def uninstall(self) -> None:
        """Remove only this guard's exact validator entry."""
        agent = self._agent_ref()
        if agent is None or self._validator_entry is None:
            return
        validators = getattr(agent, "_output_validators", None)
        if isinstance(validators, list):
            for index, entry in enumerate(validators):
                if entry is self._validator_entry:
                    validators.pop(index)
                    break
        self._validator_entry = None

    async def _validate(self, ctx: RunContext[Any], output: str) -> str:
        run = self._run_state.get()
        if run is None or ctx.partial_output:
            return output
        turn = run.recorder.turn(
            output,
            continuation_count=run.count,
            agent_generation=run.agent_generation,
        )
        run.turn = turn
        if run.ensure_active is not None:
            run.ensure_active()
        verdict = await check_finish(
            turn,
            client=run.client,
            agent_name=run.agent_name,
            target_type=run.target_type,
            target_id=run.target_id,
        )
        run.agent_generation = verdict.continuation.agent_generation
        if run.ensure_active is not None:
            run.ensure_active()
        run.verdict = verdict
        if verdict.verdict == "hold":
            run.count += 1
            raise ModelRetry(verdict.reason)
        return output

    async def run(
        self,
        prompt: str,
        recorder: TurnRecorder,
        *,
        client: NormaClient | None = None,
        agent_name: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        deps: Any = None,
        message_history: Any = None,
        _run_callable: Any = None,
        _ensure_active: Callable[[], None] | None = None,
        **run_options: Any,
    ) -> PydanticFinishResult:
        """Run a turn with a native output-retry budget matching its Norma cap."""
        agent = self._agent_ref()
        if agent is None:
            raise RuntimeError("Pydantic AI agent no longer exists")
        if self._run_state.get() is not None:
            raise RuntimeError("Cannot nest finish runs on the same Pydantic AI agent")
        run = _RunState(
            recorder=recorder,
            client=client,
            agent_name=agent_name,
            target_type=target_type,
            target_id=target_id,
            ensure_active=_ensure_active,
        )
        token = self._run_state.set(run)
        try:
            if "retries" in run_options:
                requested_retries = run_options["retries"]
                if requested_retries != recorder.max_continuations:
                    raise ValueError(
                        "Pydantic retries must match the configured finish continuation cap"
                    )
            run_options["retries"] = {"output": recorder.max_continuations}
            call = _run_callable or agent.run
            from norma.finish import use_turn_recorder

            if _ensure_active is not None:
                _ensure_active()
            with use_turn_recorder(recorder):
                result = await call(
                    prompt,
                    deps=deps,
                    message_history=message_history,
                    **run_options,
                )
            return PydanticFinishResult(
                result=result,
                verdict=run.verdict
                or _unavailable(
                    recorder.turn(
                        "",
                        continuation_count=run.count,
                        agent_generation=run.agent_generation,
                    ).continuation
                ),
                turn=run.turn
                or recorder.turn(
                    "",
                    continuation_count=run.count,
                    agent_generation=run.agent_generation,
                ),
            )
        finally:
            self._run_state.reset(token)
