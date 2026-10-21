# Modified for Norma.
"""Pydantic AI output-validator integration for Norma finish rules.

Install with ``norma-sdk[pydantic-ai]``. A reusable guard registers
exactly one validator per agent. Context-local run state prevents concurrent
turns from sharing evidence or continuation counts. Native ``ModelRetry``
retains completed tool history within the same agent run.
"""

from __future__ import annotations

import weakref
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from weakref import WeakValueDictionary

from pydantic_ai import Agent, AgentRunResult, ModelRetry, RunContext

from norma.client import NormaClient
from norma.finish import TurnRecorder, _unavailable, check_finish
from norma.models import FinishVerdict


@dataclass(frozen=True)
class PydanticFinishResult:
    """A completed framework run and its final Norma finish verdict."""

    result: AgentRunResult[str]
    verdict: FinishVerdict


@dataclass
class _RunState:
    recorder: TurnRecorder
    client: NormaClient | None
    agent_name: str | None
    target_type: str | None
    target_id: str | None
    count: int = 0
    verdict: FinishVerdict | None = None


_guards: WeakValueDictionary[int, PydanticFinishGuard] = WeakValueDictionary()


class PydanticFinishGuard:
    """Register one context-local text-output validator on a Pydantic AI agent."""

    def __new__(cls, agent: Agent[Any, str]) -> PydanticFinishGuard:
        existing = _guards.get(id(agent))
        if existing is not None and existing._agent_ref() is agent:
            return existing
        guard = super().__new__(cls)
        _guards[id(agent)] = guard
        return guard

    def __init__(self, agent: Agent[Any, str]) -> None:
        if hasattr(self, "_run_state"):
            return
        self._agent_ref = weakref.ref(agent)
        self._run_state: ContextVar[_RunState | None] = ContextVar(
            "norma_pydantic_finish_run", default=None
        )
        agent.output_validator(self._validate)

    async def _validate(self, ctx: RunContext[Any], output: str) -> str:
        run = self._run_state.get()
        if run is None or ctx.partial_output:
            return output
        verdict = await check_finish(
            run.recorder.turn(output, continuation_count=run.count),
            client=run.client,
            agent_name=run.agent_name,
            target_type=run.target_type,
            target_id=run.target_id,
        )
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
        )
        token = self._run_state.set(run)
        try:
            with recorder:
                result = await agent.run(
                    prompt,
                    deps=deps,
                    message_history=message_history,
                    retries={"output": recorder.max_continuations},
                )
            return PydanticFinishResult(
                result=result,
                verdict=run.verdict
                or _unavailable(recorder.turn("", continuation_count=run.count).continuation),
            )
        finally:
            self._run_state.reset(token)
